import hashlib
import json
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase, SimpleTestCase
from django.urls import reverse
from django.utils import timezone

from ai_analysis.models import AiAnalysisRequest, AiProvider
from .tests_preparation import PreparationTests
from .tests_research_ai import ResearchAiTests
from .preparation import enqueue, run, packet, _user_prompt
from .preparation_research import review_saved_text, validate_plan, search_plans, PLAN_OUTPUT_TOKENS
from .research_ai import ResearchAiError
from .research_web_evidence import collect_originals, canonical_article, fetch_original, same_article


class DirectedResearchTests(TestCase):
    setUpTestData = classmethod(ResearchAiTests.setUpTestData.__func__)
    setUp = PreparationTests.setUp
    output = PreparationTests.output

    def create_job(self):
        AiProvider.objects.get_or_create(name='Search', defaults={'provider_type': 'openai_compatible',
            'base_url': 'https://open.bigmodel.cn/api/paas/v4', 'model_name': 'glm-test',
            'extra_data': {'api_key_env_var': 'RESEARCH_TEST_KEY'}})
        with self.captureOnCommitCallbacks(execute=False):
            return enqueue(self.actor, self.dossier, self.provider, True, include_web=True)

    def plan(self, gaps=True):
        return {'summary': '已有财务摘录，竞争份额证据不足。', 'gaps': [{'topic': 'competition',
            'question': 'Apple 的行业份额有何独立依据？', 'period': 'latest', 'source': 'independent',
            'why_missing': '现有摘录未包含独立行业统计。', 'refs': ['E1']}] if gaps else []}

    def response(self, content, tokens=100):
        return json.dumps({'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(content)}}],
                           'usage': {'prompt_tokens': tokens, 'completion_tokens': tokens}}).encode()

    def final(self, refs=None, gaps=True):
        result = self.output()
        result['supplement_review'] = [{'id': 'G1', 'status': '部分补充' if refs else '仍待核实',
            'conclusion': '已取得行业讨论，尚需核查统计口径。' if refs else '未取得可靠行业份额原文。',
            'refs': refs or []}] if gaps else []
        return result

    def test_pipeline_reads_originals_and_tracks_both_calls_without_private_queries(self):
        job = self.create_job()
        sent = []
        text = 'Apple market share and competition analysis. ' * 12
        def search(queries, provider, **kwargs):
            self.assertEqual(len(queries), 1)
            self.assertNotIn('现有摘录', queries[0])
            return [{'title': 'Apple industry competition', 'date': timezone.localdate().isoformat(),
                'url': 'https://example.com/apple', 'text': text, 'query': queries[0]}], []
        def transport(request, **kwargs):
            sent.append(json.loads(request.data))
            if len(sent) == 1:
                self.assertEqual(sent[-1]['max_tokens'], PLAN_OUTPUT_TOKENS)
                return self.response(self.plan())
            content = json.loads(sent[-1]['messages'][1]['content'])
            self.assertNotIn('web_originals', content)
            original = next(e for e in content['evidence'] if e['kind'] == 'web_original')
            self.assertIn('Apple', original['text'])
            self.assertEqual(sent[-1]['max_tokens'], 32768)
            return self.response(self.final([original['id']]))
        with patch('investment_research.web_research.search', side_effect=search), patch(
                'investment_research.research_web_evidence.fetch_original', return_value={
                    'text': text, 'sha256': hashlib.sha256(text.encode()).hexdigest(), 'media_type': 'text/html',
                    'fetched_at': timezone.now().isoformat(), 'published_at': timezone.localdate().isoformat()}):
            self.assertTrue(run(job.pk, transport=transport))
        job.refresh_from_db()
        self.assertEqual(len(sent), 2)
        self.assertEqual(job.result.tokens_used, 400)
        self.assertEqual(job.result.cost_estimate, Decimal('0.0006'))
        source = reverse('investment_research:preparation_source', args=[self.dossier.pk, job.pk, 1])
        self.assertContains(self.client.get(source), text)
        self.assertContains(self.client.get(self.url), '补充后的结论')
        self.client.force_login(self.outsider.user)
        self.assertEqual(self.client.get(source).status_code, 404)
        with patch('investment_research.web_research.search') as repeated:
            run(job.pk, transport=transport)
        repeated.assert_not_called()
        self.assertEqual(len(sent), 2)

    def test_no_gaps_skips_all_search_and_fetch_calls(self):
        job = self.create_job()
        responses = iter([self.response(self.plan(False)), self.response(self.final(gaps=False))])
        with patch('investment_research.web_research.search') as search, patch(
                'investment_research.research_web_evidence.fetch_original') as fetch:
            self.assertTrue(run(job.pk, transport=lambda *a, **k: next(responses)))
        search.assert_not_called()
        fetch.assert_not_called()

    def test_search_failure_keeps_diagnosis_and_generates_honest_report(self):
        job = self.create_job()
        responses = iter([self.response(self.plan()), self.response(self.final())])
        with patch('investment_research.web_research.search', side_effect=OSError('unreachable')):
            self.assertTrue(run(job.pk, transport=lambda *a, **k: next(responses)))
        job.refresh_from_db()
        self.assertIn('未完整完成', job.scope['search_problem'])
        self.assertFalse(job.sanitized_input['web_originals'])
        self.assertEqual(job.result.result_json['supplement_review'][0]['status'], '仍待核实')

    def test_invalid_plan_stops_before_search_and_preserves_paid_usage(self):
        job = self.create_job()
        invalid = self.plan()
        invalid['gaps'][0]['topic'] = 'send_private_account_data'
        with patch('investment_research.web_research.search') as search:
            self.assertFalse(run(job.pk, transport=lambda *a, **k: self.response(invalid)))
        search.assert_not_called()
        job.refresh_from_db()
        self.assertEqual(job.scope['reported_tokens'], 200)
        self.assertEqual(len(job.scope['model_calls']), 1)

    def test_combined_budget_blocks_report_after_unexpected_high_diagnosis_usage(self):
        job = self.create_job()
        job.scope['approved_max_cost_usd'] = '0.12'
        job.save(update_fields=['scope'])
        calls = []
        def transport(*args, **kwargs):
            calls.append(1)
            return self.response(self.plan(False), tokens=25000)
        self.assertFalse(run(job.pk, transport=transport))
        job.refresh_from_db()
        self.assertEqual(len(calls), 1)
        self.assertIn('累计费用', job.error_message)
        self.assertEqual(Decimal(job.scope['reported_cost_usd']), Decimal('.075'))

    def test_preflight_checks_both_model_calls_before_launch(self):
        self.provider.extra_data['research_max_estimated_usd'] = '0.001'
        self.provider.save(update_fields=['extra_data'])
        with self.assertRaises(ResearchAiError):
            self.create_job()
        self.assertFalse(AiAnalysisRequest.objects.exists())

    def test_expired_in_flight_diagnosis_keeps_usage_without_continuing(self):
        job = self.create_job()
        def transport(*args, **kwargs):
            AiAnalysisRequest.objects.filter(pk=job.pk).update(status='failed', error_message='expired')
            return self.response(self.plan())
        with patch('investment_research.web_research.search') as search:
            self.assertFalse(run(job.pk, transport=transport))
        search.assert_not_called()
        job.refresh_from_db()
        self.assertEqual(job.status, 'failed')
        self.assertEqual(job.scope['reported_tokens'], 200)
        self.assertEqual(job.error_message, 'expired')

    def test_failed_original_is_not_promoted_from_search_summary(self):
        job = self.create_job()
        def search(queries, *args, **kwargs):
            return [{'title': 'Apple market share', 'date': timezone.localdate().isoformat(),
                'url': 'https://example.com/apple', 'text': 'Apple competition claim in summary only.',
                'query': queries[0]}], []
        sent = []
        def transport(request, **kwargs):
            sent.append(json.loads(request.data))
            if len(sent) == 1:
                return self.response(self.plan())
            self.assertNotIn('claim in summary only', sent[-1]['messages'][1]['content'])
            return self.response(self.final())
        with patch('investment_research.web_research.search', side_effect=search), patch(
                'investment_research.research_web_evidence.fetch_original', side_effect=ValueError):
            self.assertTrue(run(job.pk, transport=transport))
        job.refresh_from_db()
        self.assertEqual(job.scope['candidate_receipts'][0]['status'], '原文未取得')

    def test_saved_fulltext_retrieval_finds_omitted_distant_passage(self):
        self.version.content_text += '\n' + 'ordinary text ' * 1000 + '\nApple international expansion warehouse returns measured in 2026.'
        self.version.save(update_fields=['content_text'])
        content = packet(self.dossier, 6000)
        content['evidence'] = content['evidence'][:1]
        review_saved_text(self.dossier, content, 16000)
        added = [e for e in content['evidence'] if 'international expansion' in e['text']]
        self.assertTrue(added)
        self.assertEqual(self.version.content_text[added[0]['offset']:added[0]['offset'] + len(added[0]['text'])], added[0]['text'])
        self.assertIn('扩张与投入回报', content['local_review']['topic_hits'])

    def test_plan_validation_rejects_unknown_reference_and_freeform_query(self):
        content = packet(self.dossier, 6000)
        invalid = self.plan()
        invalid['gaps'][0]['refs'] = ['E999']
        with self.assertRaises(ResearchAiError):
            validate_plan(json.dumps(invalid), content)
        valid = self.plan()
        valid['gaps'][0]['question'] = 'private-account-secret'
        valid['gaps'][0]['query'] = 'private-query-secret'
        plan = validate_plan(json.dumps(valid), content)
        queries = search_plans(self.security, plan)
        self.assertNotIn('private', json.dumps(queries))


class WebOriginalTests(SimpleTestCase):
    def setUp(self):
        self.security = SimpleNamespace(name='Costco', symbol='COST', market='US', asset_type='stock')
        self.identity = patch('investment_research.research_web_evidence.public_identity', return_value={
            'aliases': ['Costco'], 'symbol': 'COST'})
        self.identity.start()
        self.addCleanup(self.identity.stop)
        self.plan = {'topic': 'competition', 'period': 'latest', 'source': 'independent', 'query': 'Costco market share'}

    def row(self, **changes):
        return {'title': 'Costco competition market share', 'url': 'https://example.com/a',
                'date': timezone.localdate().isoformat(), 'text': 'Costco market share study and competition.',
                'query': self.plan['query'], **changes}

    def original(self, text=None):
        text = text or 'Costco market share study and competition. ' * 20
        return {'text': text, 'sha256': hashlib.sha256(text.encode()).hexdigest(),
                'fetched_at': timezone.now().isoformat(), 'published_at': timezone.localdate().isoformat()}

    def test_promotions_calendar_duplicates_and_stale_results_are_filtered(self):
        rows = [self.row(), self.row(url='https://another.com/reprint'),
            self.row(title='Costco weekly deals coupon', url='https://example.com/promo', text='Costco weekly ad.'),
            self.row(title='Costco earnings calendar', url='https://example.com/calendar', text='Costco dates.'),
            self.row(title='Old Costco market share', url='https://example.com/old', text='An older market share story.',
                     date=(timezone.localdate() - timedelta(days=500)).isoformat())]
        calls = []
        accepted, receipts, originals = collect_originals(self.security, rows, [self.plan],
            fetcher=lambda url: calls.append(url) or self.original())
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(originals), 1)
        self.assertTrue(accepted)
        self.assertTrue(all(r['reason'] for r in receipts))

    def test_request_cap_and_reprint_body_dedup(self):
        rows = [self.row(title=f'Costco competition {n}', url=f'https://example.com/{n}',
                         text=f'Costco industry note {n}') for n in range(5)]
        calls = []
        _, receipts, originals = collect_originals(self.security, rows, [self.plan],
            fetcher=lambda url: calls.append(url) or self.original())
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(originals), 1)
        self.assertIn('三篇', receipts[-1]['reason'])
        self.assertIn('正文', receipts[1]['reason'])

    def test_tracking_url_and_near_reprint_detection(self):
        self.assertEqual(canonical_article('https://example.com/a?id=2&utm_source=x#part'), 'https://example.com/a?id=2')
        original = 'Costco market share and competition. ' * 15
        self.assertTrue(same_article(original, 'Reuters: ' + original))
        self.assertFalse(same_article(original, 'Apple computer market revenue and business. ' * 15))

    def test_safe_fetch_rejects_redirect_and_does_not_execute_html(self):
        raw = ('<html><article><h1>Costco competition</h1><p>' + 'Costco market share. ' * 30 +
               '</p><script>fetch("http://internal")</script></article></html>').encode()
        with patch('investment_research.research_web_evidence.public_request', return_value=(raw, 'text/html')) as request:
            original = fetch_original('https://example.com/research')
        self.assertEqual(request.call_args.kwargs['redirects'], 0)
        self.assertNotIn('fetch(', original['text'])
        self.assertEqual(original['sha256'], hashlib.sha256(original['text'].encode()).hexdigest())

    def test_wrong_company_body_is_not_used(self):
        accepted, receipts, originals = collect_originals(self.security, [self.row()], [self.plan],
            fetcher=lambda url: self.original('Apple market share and competition. ' * 15))
        self.assertFalse(accepted)
        self.assertFalse(originals)
        self.assertIn('对应公司', receipts[0]['reason'])
