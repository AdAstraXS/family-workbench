import json
import tempfile
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, TransactionTestCase, override_settings
from django.db import connection, connections, close_old_connections
from django.urls import reverse
from django.utils import timezone

from family_core.models import Family, FamilyMember
from knowledge.crypto import encrypt_json
from knowledge.models import KnowledgeDocument
from ai_analysis.models import AiProvider
from .program_models import ProgramSubscription, ProgramSettings, ProgramEntry, ProgramRevision, ProgramSummaryChunk
from .program_processing import save_revision, process_entry, submit_asr, validate_points, make_chunks, store_audio, audio_access_url
from .program_sources import ProgramError, parse_catalogue_feed, collect_subscription
from .program_media import parse_asr_result, asr_request
from .program_archive import archive_program


@override_settings(KNOWLEDGE_TOKEN_ENCRYPTION_KEY=Fernet.generate_key().decode())
class ProgramTests(TestCase):
    def setUp(self):
        self.media = tempfile.TemporaryDirectory()
        self.override = override_settings(MEDIA_ROOT=self.media.name)
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.addCleanup(self.media.cleanup)
        self.family = Family.objects.create(name='精选家庭')
        self.other = Family.objects.create(name='另一家庭')
        self.user = get_user_model().objects.create_user(username='program-admin')
        self.member = FamilyMember.objects.create(family=self.family, user=self.user, display_name='管理员', role='admin')
        self.sub = ProgramSubscription.objects.create(family=self.family, code='good_company')
        self.entry = ProgramEntry.objects.create(subscription=self.sub, external_id='episode-1', title='CEO 访谈',
            url='https://shows.acast.com/example', audio_url='https://media.acast.com/test.mp3', duration_seconds=1500)
        self.config = ProgramSettings.objects.create(family=self.family, allow_asr=True,
            encrypted_credentials=encrypt_json({'api_key': 'test-not-real'}))
        self.client.force_login(self.user)

    def revision(self, text='微软的投资需要关注现金流。'):
        return save_revision(self.entry, [{'text': text, 'start_ms': 1000, 'end_ms': 2000}], origin='fun-asr', source_url=self.entry.url)

    def test_revision_idempotent_and_new_version_preserves_original(self):
        first = self.revision()
        self.assertEqual(self.revision().pk, first.pk)
        second = self.revision('另一份完整原文。')
        first.refresh_from_db()
        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual(first.text, '微软的投资需要关注现金流。')
        self.assertEqual(self.entry.revisions.count(), 2)

    def test_get_pages_do_not_create_rows_or_network_calls(self):
        self.config.delete()
        with patch('intelligence.program_sources.fetch_public_url') as fetch:
            for name, args in [('program_list', []), ('program_settings', []), ('program_detail', [self.entry.pk])]:
                self.assertEqual(self.client.get(reverse('intelligence:' + name, args=args)).status_code, 200)
        self.assertEqual(ProgramSettings.objects.count(), 0)
        self.assertEqual(ProgramSubscription.objects.count(), 1)
        fetch.assert_not_called()

    def test_family_isolation_and_member_settings_permission(self):
        other_sub = ProgramSubscription.objects.create(family=self.other, code='oaktree')
        foreign = ProgramEntry.objects.create(subscription=other_sub, external_id='x', title='秘密标题', url='https://example.com')
        self.assertNotContains(self.client.get(reverse('intelligence:program_list')), '秘密标题')
        self.assertEqual(self.client.get(reverse('intelligence:program_detail', args=[foreign.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse('intelligence:program_action', args=[foreign.pk]), {'action': 'retry'}).status_code, 404)
        self.member.role = 'member'
        self.member.save()
        self.assertEqual(self.client.get(reverse('intelligence:program_settings')).status_code, 403)

    def test_viewer_cannot_archive(self):
        revision = self.revision()
        self.member.role = 'viewer'
        self.member.save()
        self.assertEqual(self.client.post(reverse('intelligence:program_action', args=[self.entry.pk]), {'action': 'archive', 'revision_id': revision.pk}).status_code, 403)

    def test_settings_encrypts_key_and_never_renders_it(self):
        response = self.client.post(reverse('intelligence:program_settings'), {'action': 'settings', 'api_key': 'secret-value-for-test',
            'allow_asr': 'on', 'max_audio_minutes': 180, 'monthly_asr_cny': 20, 'monthly_summary_usd': 2})
        self.assertEqual(response.status_code, 302)
        self.config.refresh_from_db()
        self.assertNotIn('secret-value-for-test', self.config.encrypted_credentials)
        self.assertNotContains(self.client.get(reverse('intelligence:program_settings')), 'secret-value-for-test')

    def test_catalogue_selection_and_pause(self):
        self.client.post(reverse('intelligence:program_settings'), {'action': 'subscriptions', 'sources': ['dwarkesh', 'oaktree'], 'auto': ['oaktree']})
        self.sub.refresh_from_db()
        self.assertFalse(self.sub.enabled)
        self.assertEqual(ProgramSubscription.objects.filter(enabled=True).count(), 2)

    def test_rss_excludes_short_versions_and_does_not_treat_description_as_transcript(self):
        body = b'<rss><channel><item><title>HIGHLIGHTS: CEO</title><link>https://example.com/short</link></item><item><title>CEO complete</title><link>https://example.com/full</link><description>Transcript short description</description><enclosure url="https://example.com/audio.mp3"/><duration>25:20</duration></item></channel></rss>'
        rows = parse_catalogue_feed('good_company', body)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['duration_seconds'], 1520)
        self.assertEqual(rows[0]['segments'], [])

    def test_oaktree_embedded_index_deduplicates(self):
        data = json.dumps([{'Title': 'Memo', 'MoreLink': '/insights/memo/one', 'IsoDate': '2026-09-01T00:00:00Z'}]).replace('"', '&quot;')
        rows = parse_catalogue_feed('oaktree', f'<div data-items="{data}"></div><div data-items="{data}"></div>'.encode())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['published_at'].month, 9)

    def test_first_collection_only_latest_automatically_processed(self):
        items = [dict(external_id=str(i), title=str(i), url=f'https://example.com/{i}') for i in range(5)]
        with patch('intelligence.program_sources.fetch_public_url', return_value=SimpleNamespace(body=b'')), patch('intelligence.program_sources.parse_catalogue_feed', side_effect=lambda *args: [dict(i) for i in items]):
            collect_subscription(self.sub)
            self.assertEqual(self.sub.entries.filter(requested=True).count(), 2)  # existing entry and newest
            self.assertEqual(self.sub.entries.count(), 4)
            self.sub.refresh_from_db()
            collect_subscription(self.sub)
            self.assertEqual(self.sub.entries.count(), 6)
            self.assertEqual(self.sub.entries.filter(requested=True).count(), 2)

    def test_asr_submission_saved_and_repeated_worker_only_polls(self):
        with patch('intelligence.program_processing.asr_request', return_value={'output': {'task_id': 'task-1'}}) as remote:
            submit_asr(self.entry, self.config)
            self.entry.refresh_from_db()
            self.assertEqual(self.entry.state, 'asr_wait')
            self.assertEqual(self.entry.asr_reserved_cny, Decimal('.3300'))
            with self.assertRaises(ProgramError):
                submit_asr(self.entry, self.config)
            self.assertEqual(remote.call_count, 1)
        with patch('intelligence.program_processing.asr_request', return_value={'output': {'task_status': 'RUNNING'}}) as remote:
            process_entry(self.entry.pk)
            self.assertEqual(remote.call_args.kwargs, {'task_id': 'task-1'})

    def test_uncertain_submission_is_not_automatically_resubmitted(self):
        with patch('intelligence.program_processing.asr_request', side_effect=ProgramError('服务请求失败')) as remote:
            self.assertFalse(process_entry(self.entry.pk))
            self.entry.refresh_from_db()
            self.assertEqual(self.entry.state, 'uncertain')
            self.assertFalse(process_entry(self.entry.pk))
            self.assertEqual(remote.call_count, 1)
        self.assertEqual(self.entry.asr_reserved_cny, Decimal('.3300'))

    def test_budget_blocks_network(self):
        self.config.monthly_asr_cny = Decimal('.10')
        self.config.save()
        with patch('intelligence.program_processing.asr_request') as remote:
            with self.assertRaises(ProgramError):
                submit_asr(self.entry, self.config)
            remote.assert_not_called()

    def test_existing_lease_blocks_worker_and_manual_retry(self):
        self.entry.lease_until = timezone.now() + timedelta(minutes=10)
        self.entry.save()
        with patch('intelligence.program_processing.asr_request') as remote:
            self.assertFalse(process_entry(self.entry.pk))
            self.client.post(reverse('intelligence:program_action', args=[self.entry.pk]), {'action': 'retry'})
            remote.assert_not_called()

    def test_asr_result_validates_timestamps(self):
        result = {'transcripts': [{'sentences': [{'text': 'hello', 'begin_time': 0, 'end_time': 100}]}]}
        self.assertEqual(parse_asr_result(result)[0]['end_ms'], 100)
        result['transcripts'][0]['sentences'][0]['begin_time'] = 200
        with self.assertRaises(ProgramError):
            parse_asr_result(result)

    def test_completed_asr_saves_text_not_signed_urls(self):
        self.entry.task_id, self.entry.state = 'task-1', 'asr_wait'
        self.entry.save()
        with patch('intelligence.program_processing.asr_request', return_value={'output': {'task_status': 'SUCCEEDED'}}), patch('intelligence.program_processing.download_asr_result', return_value=[{'text': '完整句子', 'start_ms': 0, 'end_ms': 1000}]):
            self.assertTrue(process_entry(self.entry.pk))
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.current_revision.text, '完整句子')
        self.assertEqual(self.entry.state, 'text_ready')

    def test_no_ai_without_explicit_fulltext_authorization(self):
        self.revision()
        with patch('intelligence.program_processing.private_json_request') as remote:
            process_entry(self.entry.pk)
            remote.assert_not_called()

    def test_summary_rejects_invented_refs(self):
        item = {'points': [{'topic': 'MSFT', 'kind': '作者观点', 'text': '示例', 'refs': [99]}]}
        with self.assertRaises(ProgramError):
            validate_points(item, {1, 2})
        item['points'][0]['refs'] = [1]
        self.assertEqual(validate_points(item, {1})['points'][0]['refs'], [1])

    def test_chunking_covers_all_paragraphs(self):
        chunks = make_chunks([{'text': '字' * 80} for _ in range(10)], 250)
        self.assertEqual([r['id'] for c in chunks for r in c], list(range(1, 11)))
        self.assertGreater(len(chunks), 1)

    def test_archive_is_manual_versioned_and_idempotent(self):
        revision = self.revision()
        self.assertEqual(KnowledgeDocument.objects.count(), 0)
        document = archive_program(revision, self.member)
        self.assertEqual(archive_program(revision, self.member).pk, document.pk)
        self.assertIn(revision.text, document.current_revision.plain_text)
        self.assertEqual(document.knowledge_status, KnowledgeDocument.KNOWLEDGE_PENDING)
        new_revision = self.revision('更新后的观点')
        self.assertNotEqual(archive_program(new_revision, self.member).pk, document.pk)

    def test_audio_encrypted_expiring_capability(self):
        store_audio(self.entry, b'test-audio-bytes', 'audio/mp4')
        with self.entry.audio_file.open('rb') as file:
            self.assertNotIn(b'test-audio-bytes', file.read())
        self.config.public_base_url = 'https://example.com:8443'
        url = audio_access_url(self.entry, self.config)
        path = url.split('example.com:8443')[1]
        self.client.logout()
        self.assertEqual(self.client.get(path).status_code, 404)
        ProgramEntry.objects.filter(pk=self.entry.pk).update(state='asr_wait')
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b''.join(response.streaming_content), b'test-audio-bytes')
        ProgramEntry.objects.filter(pk=self.entry.pk).update(audio_expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.client.get(path).status_code, 404)

    def test_program_detail_escapes_source_content(self):
        self.revision('<script>alert(1)</script> source')
        response = self.client.get(reverse('intelligence:program_detail', args=[self.entry.pk]))
        self.assertNotContains(response, '<script>alert(1)</script>')
        self.assertContains(response, '&lt;script&gt;')

    @patch.dict('os.environ', {'PROGRAM_TEST_API_KEY': 'test-only'})
    def test_long_summary_resumes_successful_parts_and_keeps_audit_cost(self):
        provider = AiProvider.objects.create(name='Test', provider_type='openai_compatible', model_name='test-model', base_url='https://example.com/v1', extra_data={
            'allow_intelligence_analysis': True, 'intelligence_data_scope': 'public_metadata_only',
            'intelligence_policy_version': 'public-metadata-v1', 'intelligence_policy_reviewed_on': '2026-09-26',
            'intelligence_max_input_characters': 2000, 'intelligence_max_output_tokens': 512,
            'intelligence_input_usd_per_million': '.1', 'intelligence_output_usd_per_million': '.2',
            'intelligence_max_estimated_usd': '.1', 'api_key_env_var': 'PROGRAM_TEST_API_KEY'})
        self.config.summary_provider, self.config.allow_summary = provider, True
        self.config.save()
        revision = save_revision(self.entry, [{'text': str(i) + '公开文字。' * 100} for i in range(8)], origin='publisher', source_url=self.entry.url)
        def response(url, *, key, payload):
            data = json.loads(payload['messages'][1]['content'])
            point = {'topic': 'MSFT', 'kind': '作者观点', 'text': '仅用于验证的摘要', 'refs': [data['paragraphs'][0]['id']]}
            return {'choices': [{'message': {'content': json.dumps({'points': [point]})}, 'finish_reason': 'stop'}], 'usage': {'total_tokens': 100}}
        with patch('intelligence.ai_enrichment._chat_url', return_value='https://example.com/v1/chat/completions'), patch('intelligence.program_processing.private_json_request', side_effect=response) as remote:
            for _ in range(12):
                process_entry(self.entry.pk)
            calls = remote.call_count
            self.assertGreater(calls, 1)
            self.entry.refresh_from_db()
            self.assertEqual(self.entry.state, 'ready')
            process_entry(self.entry.pk)
            self.assertEqual(remote.call_count, calls)
        revision.refresh_from_db()
        self.assertTrue(revision.summary_complete)
        self.assertTrue(all(c.reserved_usd > 0 and c.model_name == 'test-model' for c in revision.chunks.all()))

    def test_paused_subscription_does_not_run(self):
        self.sub.enabled = False
        self.sub.save()
        with patch('intelligence.program_processing.asr_request') as remote:
            self.assertFalse(process_entry(self.entry.pk))
            remote.assert_not_called()

    def test_waiting_for_key_resumes_after_configuration(self):
        self.config.allow_asr = False
        self.config.save()
        self.assertFalse(process_entry(self.entry.pk))
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.state, 'waiting_config')
        self.assertIsNone(self.entry.submitted_at)
        self.config.allow_asr = True
        self.config.save()
        with patch('intelligence.program_processing.asr_request', return_value={'output': {'task_id': 'resumed-task'}}) as remote:
            self.assertTrue(process_entry(self.entry.pk))
            self.assertEqual(remote.call_count, 1)

    def test_later_summary_archive_preserves_previous_knowledge_version(self):
        revision = self.revision()
        document = archive_program(revision, self.member)
        original_revision = document.current_revision
        revision.refresh_from_db()
        revision.summary = {'points': [{'topic': 'MSFT', 'kind': '作者观点', 'text': '后续整理', 'refs': [1]}]}
        revision.summary_complete = True
        revision.save()
        updated = archive_program(revision, self.member)
        self.assertEqual(document.pk, updated.pk)
        self.assertEqual(updated.current_revision.revision_number, 2)
        self.assertNotIn('后续整理', original_revision.plain_text)
        self.assertIn('后续整理', updated.current_revision.plain_text)

    def test_stock_filter_does_not_match_intelligence_as_intel(self):
        self.revision('Artificial intelligence improves Microsoft products.')
        response = self.client.get(reverse('intelligence:program_list'), {'stock': 'INTC'})
        self.assertNotContains(response, 'CEO 访谈')
        response = self.client.get(reverse('intelligence:program_list'), {'stock': 'MSFT'})
        self.assertContains(response, 'CEO 访谈')


@override_settings(KNOWLEDGE_TOKEN_ENCRYPTION_KEY=Fernet.generate_key().decode())
class ProgramBudgetConcurrencyTests(TransactionTestCase):
    def test_parallel_reservations_cannot_exceed_family_budget(self):
        if connection.vendor != 'postgresql':
            self.skipTest('Row-lock concurrency requires PostgreSQL.')
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        family = Family.objects.create(name='并发预算测试')
        sub = ProgramSubscription.objects.create(family=family, code='good_company')
        config = ProgramSettings.objects.create(family=family, allow_asr=True, monthly_asr_cny=Decimal('.50'),
            encrypted_credentials=encrypt_json({'api_key': 'test-only'}))
        ids = [ProgramEntry.objects.create(subscription=sub, external_id=str(i), title='test', url='https://example.com',
            audio_url='https://example.com/audio.mp3', duration_seconds=1500).pk for i in range(2)]
        barrier = Barrier(2)
        def run(pk):
            close_old_connections()
            try:
                entry = ProgramEntry.objects.select_related('subscription').get(pk=pk)
                local_config = ProgramSettings.objects.get(pk=config.pk)
                barrier.wait(timeout=10)
                submit_asr(entry, local_config)
                return True
            except ProgramError:
                return False
            finally:
                connections.close_all()
        with patch('intelligence.program_processing.asr_request', return_value={'output': {'task_id': 'test-task'}}) as remote:
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(run, ids))
            self.assertEqual(sum(results), 1)
            self.assertEqual(remote.call_count, 1)
        self.assertEqual(ProgramEntry.objects.filter(state='asr_wait').count(), 1)
