from django.test import TestCase, Client
from django.urls import reverse
from django.db import connection
from django.test.utils import CaptureQueriesContext
from ai_analysis.models import AiAnalysisRequest
from . import tests_thesis_analysis as fixtures
from .models import ResearchPreparation, ResearchThesisRevision
from .research_basis import basis_matches
from .research_ai import ResearchAiError


class NavigationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        fixtures.ThesisAnalysisTests.setUpTestData.__func__(cls)

    response = staticmethod(fixtures.ThesisAnalysisTests.response)
    generate = fixtures.ThesisAnalysisTests.generate

    def setUp(self):
        self.client.force_login(self.user)

    def path(self, name):
        return reverse('investment_research:' + name, args=[self.dossier.pk])

    def test_all_primary_and_auxiliary_pages_share_shell_and_are_read_only(self):
        for route in ['prepare', 'company_research', 'detail', 'follow', 'metric_focus', 'review_plan',
                      'filing_reviews', 'materials', 'library_news', 'research_history', 'valuation',
                      'financials', 'futu_financials', 'next_day_tracking', 'documents']:
            with self.subTest(route=route), CaptureQueriesContext(connection) as queries:
                response = self.client.get(self.path(route))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'aria-label="公司研究流程"', count=1)
                self.assertContains(response, 'aria-label="投研当前位置"', count=1)
                self.assertFalse(any(q['sql'].lstrip().upper().startswith(('INSERT ', 'UPDATE ', 'DELETE ')) for q in queries))
                html = response.content.decode()
                stages = html.split('aria-label="公司研究流程"')[1].split('</nav>')[0]
                self.assertEqual(stages.count('aria-current="step"'), 0 if route in
                    ['materials', 'library_news', 'financials', 'futu_financials', 'valuation', 'documents'] else 1)

    def test_observation_is_private_post_only_and_does_not_change_judgment(self):
        before = self.dossier.current_revision_id
        url = self.path('observation')
        self.assertEqual(self.client.get(url).status_code, 405)
        self.client.force_login(self.other_user)
        self.assertEqual(self.client.post(url, {'enabled': '1'}).status_code, 404)
        self.client.force_login(self.user)
        response = self.client.post(url, {'enabled': '1', 'return_to': 'https://example.com/'})
        self.assertRedirects(response, self.path('follow'))
        self.dossier.refresh_from_db()
        self.assertTrue(self.dossier.is_watched)
        self.assertEqual(self.dossier.current_revision_id, before)
        self.assertContains(self.client.get(reverse('investment_research:index'), {'filter': 'watch'}), self.security.name)
        self.client.post(url, {'enabled': '0'})
        self.assertNotContains(self.client.get(reverse('investment_research:index'), {'filter': 'watch'}), self.security.name)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.user)
        self.assertEqual(csrf_client.post(url, {'enabled': '1'}).status_code, 403)

    def test_candidate_research_freezes_basis_without_creating_formal_judgment(self):
        self.dossier.current_revision = None
        self.dossier.save(update_fields=['current_revision'])
        job = AiAnalysisRequest.objects.create(family=self.actor.family, member=self.actor,
            module='investment_research', analysis_type='company_introduction', status='success',
            scope={'dossier_id': self.dossier.pk})
        prep = ResearchPreparation.objects.create(dossier=self.dossier, analysis=job,
            questions=['现金能否跟上？'], hypotheses=[{'claim': '需求会持续增长'}], decision='research')
        before = ResearchThesisRevision.objects.count()
        report = self.generate()
        self.assertIsNone(report.scope['thesis_revision_id'])
        self.assertEqual(report.scope['preparation_id'], prep.pk)
        self.assertEqual(report.scope['research_basis'], 'candidate_hypotheses')
        self.assertEqual(ResearchThesisRevision.objects.count(), before)
        self.assertTrue(basis_matches(self.dossier, report.scope))
        prep.revision += 1
        prep.save()
        self.assertFalse(basis_matches(self.dossier, report.scope))
        with self.assertRaisesMessage(ResearchAiError, '完整重评'):
            self.generate(review_mode='incremental')

    def test_company_switcher_never_exposes_other_members_and_uses_parent_routes(self):
        report = self.generate()
        response = self.client.get(reverse('investment_research:thesis_analysis_detail', args=[self.dossier.pk, report.pk]))
        html = response.content.decode().split('data-research-company')[1].split('</select>')[0]
        self.assertIn(self.path('research_history'), html)
        self.assertNotIn(f'/analysis/{report.pk}/', html)
