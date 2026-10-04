from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from . import tests_research_ai as fixtures
from .services import create_exploration
from portfolio.models import Security


class ResearchPageEfficiencyTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        fixtures.ResearchAiTests.setUpTestData.__func__(cls)

    def test_runtime_failure_preserves_completed_acquisition_items(self):
        from django.utils import timezone
        from family_core.job_runtime import record_failure
        from .models import CompanyAcquisitionJob
        completed = {'title': 'Already collected', 'status': 'success'}
        job = CompanyAcquisitionJob.objects.create(dossier=self.dossier, status='running',
            expires_at=timezone.now(), items=[completed])
        record_failure('run_company_acquisition', {'job_id': job.pk}, 'timed out', started=True)
        job.refresh_from_db()
        self.assertEqual(job.status, 'failed')
        self.assertEqual(job.items[0], completed)
        self.assertEqual(job.items[1]['message'], 'timed out')

    def test_search_and_pagination_keep_queries_bounded(self):
        self.client.force_login(self.user)
        for i in range(50):
            create_exploration(actor=self.actor, security=Security.objects.create(symbol=f'EFF{i}',name=f'Efficiency {i}',market='US'))
        create_exploration(actor=self.outsider, security=self.security)
        url = reverse('investment_research:index')
        self.client.get(url)
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(url)
        self.assertEqual(len(response.context['page']), 20)
        self.assertEqual(response.context['page'].paginator.count, 51)
        self.assertLessEqual(len(queries), 12)
        self.assertFalse(any(q['sql'].lstrip().startswith(('INSERT','UPDATE','DELETE')) for q in queries))
        response = self.client.get(url, {'q':'NO_MATCH'})
        self.assertEqual(response.context['page'].paginator.count, 0)
        self.assertEqual(len(response.context['page']), 0)
        response = self.client.get(url, {'q':'EFF49'})
        self.assertEqual(response.context['page'].paginator.count, 1)
