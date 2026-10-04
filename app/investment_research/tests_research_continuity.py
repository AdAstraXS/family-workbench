import importlib
from types import SimpleNamespace
from unittest.mock import Mock, patch
from django.apps import apps
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from ai_analysis.models import AiAnalysisRequest, AiAnalysisResult
from . import tests_research_ai as fixtures
from .models import ResearchThesisRevision, ResearchQuestion, ResearchQuestionUpdate, CompanyMaterial
from .material_store import save_material
from . import sec_library


class ResearchContinuityTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        fixtures.ResearchAiTests.setUpTestData.__func__(cls)

    def setUp(self):
        self.client.force_login(self.user)

    def migrate_questions(self):
        migration = importlib.import_module('investment_research.migrations.0021_import_latest_confirmed_questions')
        migration.import_questions(apps, SimpleNamespace(connection=connection))

    def legacy_revision(self):
        revision = ResearchThesisRevision.objects.create(dossier=self.dossier, revision_number=1,
            thesis='旧判断', questions=['资本开支回报如何？', '盈利指引如何？'], pillars=['旧假设'], created_by=self.actor)
        self.dossier.current_revision = revision
        self.dossier.save(update_fields=['current_revision'])
        return revision

    def analysis(self, revision, member=None):
        job = AiAnalysisRequest.objects.create(member=member or self.actor, family=(member or self.actor).family,
            module='investment_research', analysis_type='thesis_synthesis', provider=self.provider, status='success',
            scope={'dossier_id': self.dossier.pk, 'thesis_revision_id': revision.pk}, finished_at=timezone.now())
        AiAnalysisResult.objects.create(request=job, result_text='历史分析', result_json={'assessments': [
            {'kind': 'question', 'index': 0, 'text': '资本开支回报如何？', 'verdict': 'unknown',
             'reason': '尚缺回报率', 'detail': '资本开支上升', 'boundary': '无法验证回报率'},
            {'kind': 'pillar', 'index': 0, 'verdict': 'supports', 'reason': '不能冒充问题的答案'}]})
        return job

    def test_import_preserves_history_dates_and_only_confirmed_questions(self):
        revision = self.legacy_revision()
        job = self.analysis(revision)
        self.migrate_questions()
        self.migrate_questions()
        self.assertEqual(ResearchQuestion.objects.count(), 2)
        self.assertEqual(ResearchQuestionUpdate.objects.count(), 1)
        update = ResearchQuestionUpdate.objects.get()
        self.assertEqual(update.created_at, job.finished_at)
        self.assertEqual(update.analysis_id, job.pk)
        self.assertIn('/analysis/', update.evidence[0]['url'])
        response = self.client.get(reverse('investment_research:follow', args=[self.dossier.pk]))
        self.assertContains(response, '历史分析 · 原核查')
        self.assertContains(response, '尚缺回报率')
        self.assertContains(response, '盈利指引如何？')
        self.assertEqual(ResearchThesisRevision.objects.get().questions, revision.questions)
        self.assertEqual(AiAnalysisRequest.objects.count(), 1)

    def test_wrong_owner_analysis_is_not_imported(self):
        self.analysis(self.legacy_revision(), self.outsider)
        self.migrate_questions()
        self.assertEqual(ResearchQuestion.objects.count(), 2)
        self.assertFalse(ResearchQuestionUpdate.objects.exists())

    def test_only_latest_confirmed_revision_is_imported(self):
        old = self.legacy_revision()
        self.analysis(old)
        latest = ResearchThesisRevision.objects.create(dossier=self.dossier, revision_number=2,
            thesis='最新判断', questions=['最新问题'], created_by=self.actor)
        self.dossier.current_revision = latest
        self.dossier.save(update_fields=['current_revision'])
        self.migrate_questions()
        self.assertEqual(list(ResearchQuestion.objects.values_list('title', flat=True)), ['最新问题'])
        self.assertFalse(ResearchQuestionUpdate.objects.exists())

    def test_import_does_not_exceed_tracking_limit_or_enable_auto_ai(self):
        from .models import ResearchAutoDigestConsent
        from investment_watch.models import WatchConsent
        self.legacy_revision()
        for i in range(10):
            ResearchQuestion.objects.create(dossier=self.dossier, title=f'新版问题{i}')
        consent = ResearchAutoDigestConsent.objects.create(dossier=self.dossier, authorized_by=self.actor, provider=self.provider)
        self.migrate_questions()
        self.assertEqual(ResearchQuestion.objects.filter(status='tracking').count(), 10)
        self.assertEqual(ResearchQuestion.objects.filter(status='paused').count(), 2)
        consent.refresh_from_db()
        self.assertIsNotNone(consent.revoked_at)
        self.assertFalse(WatchConsent.objects.filter(dossier=self.dossier, active=True).exists())

    def test_active_materials_page_uses_light_polling_without_full_refresh(self):
        from datetime import timedelta
        from .models import CompanyAcquisitionJob
        CompanyAcquisitionJob.objects.create(dossier=self.dossier, status='running',
            selection=['sec'], expires_at=timezone.now() + timedelta(minutes=10))
        response = self.client.get(reverse('investment_research:materials', args=[self.dossier.pk]) + '?tab=acquisition')
        self.assertNotContains(response, 'http-equiv="refresh"')
        self.assertContains(response, "?format=progress")
        self.assertContains(response, '稍后自动重新连接')

    def test_existing_new_or_removed_questions_are_not_overwritten(self):
        self.legacy_revision()
        old = ResearchQuestion.objects.create(dossier=self.dossier, title='资本开支回报如何？', status='removed', metrics='用户新版口径')
        self.migrate_questions()
        old.refresh_from_db()
        self.assertEqual(old.status, 'removed')
        self.assertEqual(old.metrics, '用户新版口径')
        self.assertEqual(ResearchQuestion.objects.count(), 2)

    def test_progress_does_not_build_material_inventory(self):
        with patch('investment_research.material_views.inventory', side_effect=AssertionError('heavy inventory')):
            response = self.client.get(reverse('investment_research:materials', args=[self.dossier.pk]) + '?format=progress')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'active': False, 'items': []})

    def test_material_table_never_reads_original_blobs_or_full_official_text(self):
        save_material(self.security, 'filing', 'sec_document', 'x' * 300, text='正文' * 5000,
            source_url='https://www.sec.gov/other.htm', data={'filing_date': '2026-09-27'})
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(reverse('investment_research:materials', args=[self.dossier.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'rw-material-title')
        self.assertContains(response, '2026-09-27')
        self.assertFalse(any('raw_gzip' in q['sql'] for q in queries))
        for q in queries:
            if 'officialresearchcontentversion' in q['sql']:
                self.assertIn('SUBSTR', q['sql'].upper())
                self.assertNotIn('"officialresearchcontentversion"."content_text",', q['sql'])

    def test_sec_repeat_download_uses_saved_primary_directory_and_exhibits(self):
        record = {'accession': '0000320193-26-000001', 'primary_document': 'apple.htm',
                  'title': 'Apple 10-K', 'document_type': '10-k', 'filing_date': '2026-09-27'}
        client = Mock()
        client.get_json.return_value = {'directory': {'item': [{'name': 'apple.htm'}, {'name': 'ex99.htm'}]}}
        client.get_document_html.return_value = b'<html><body>Financial report original.</body></html>'
        sec_library.download(self.security, '0000320193', record, client)
        # Primary was already in the official archive: only the missing exhibit downloads.
        self.assertEqual(client.get_document_html.call_count, 1)
        count = CompanyMaterial.objects.count()
        client.reset_mock()
        message = sec_library.download(self.security, '0000320193', record, client)
        client.get_json.assert_not_called()
        client.get_document_html.assert_not_called()
        self.assertIn('补取 0 份', message)
        self.assertEqual(CompanyMaterial.objects.count(), count)

    def test_sec_missing_attachment_retries_without_redownloading_primary(self):
        record = {'accession': '0000320193-26-000002', 'primary_document': 'main.htm', 'title': 'Annual', 'document_type': '10-k'}
        client = Mock()
        client.get_json.return_value = {'directory': {'item': [{'name': 'ex99.htm'}]}}
        client.get_document_html.side_effect = [b'<html>Saved primary report.</html>', ValueError('unavailable')]
        with self.assertRaises(ValueError):
            sec_library.download(self.security, '0000320193', record, client)
        client.reset_mock()
        client.get_document_html.side_effect = None
        client.get_document_html.return_value = b'<html>Now saved exhibit.</html>'
        sec_library.download(self.security, '0000320193', record, client)
        client.get_json.assert_not_called()
        self.assertEqual(client.get_document_html.call_count, 1)
        self.assertTrue(client.get_document_html.call_args.args[0].endswith('ex99.htm'))

    def test_new_sec_amendment_is_still_downloaded(self):
        record = {'accession': '0000320193-26-000003', 'primary_document': 'amended.htm', 'title': '10-K/A', 'document_type': '10-k'}
        client = Mock()
        client.get_json.return_value = {'directory': {'item': []}}
        client.get_document_html.return_value = b'<html>New amended report.</html>'
        sec_library.download(self.security, '0000320193', record, client)
        client.get_document_html.assert_called_once()
