import json
import os
from decimal import Decimal
from unittest.mock import patch
from django.test import TestCase, SimpleTestCase
from django.urls import reverse
from ai_analysis.models import AiProvider
from .tests_preparation import PreparationTests
from .tests_research_ai import ResearchAiTests
from .preparation import enqueue, run, SYSTEM
from .prompt_settings import effective_prompt, save_template
from .research_ai import ResearchAiError, report_policy
from .web_research import public_link, queries_for, search


class ReportFixTests(TestCase):
    setUpTestData = classmethod(ResearchAiTests.setUpTestData.__func__)
    setUp = PreparationTests.setUp
    output = PreparationTests.output
    job = PreparationTests.job

    def test_report_default_is_high_and_explicit_override_is_respected(self):
        self.assertEqual(report_policy(self.provider)['max_output_tokens'], 32768)
        self.provider.extra_data['research_report_output_tokens'] = 65536
        self.assertEqual(report_policy(self.provider)['max_output_tokens'], 65536)
        self.provider.extra_data['research_report_output_tokens'] = True
        with self.assertRaises(ResearchAiError):
            report_policy(self.provider)

    def test_template_override_revision_and_member_isolation(self):
        save_template(self.actor, self.dossier, 'default', '核查会员续费', 0)
        self.assertIn('核查会员续费', effective_prompt(self.dossier, SYSTEM)[0])
        save_template(self.actor, self.dossier, 'company', '核查门店增长', 0)
        self.assertIn('核查门店增长', effective_prompt(self.dossier, SYSTEM)[0])
        self.assertNotIn('核查会员续费', effective_prompt(self.dossier, SYSTEM)[0])
        with self.assertRaises(ResearchAiError):
            save_template(self.actor, self.dossier, 'company', '覆盖', 0)
        with self.assertRaises(ResearchAiError):
            save_template(self.outsider, self.dossier, 'company', '越权', 1)
        save_template(self.actor, self.dossier, 'company', '', 1)
        self.assertIn('核查会员续费', effective_prompt(self.dossier, SYSTEM)[0])

    def test_job_uses_frozen_prompt_and_records_length_failure_usage(self):
        save_template(self.actor, self.dossier, 'company', '原来的研究重点', 0)
        job = self.job(False)
        save_template(self.actor, self.dossier, 'company', '后来修改的重点', 1)
        def transport(request, **kwargs):
            payload = json.loads(request.data)
            self.assertIn('原来的研究重点', payload['messages'][0]['content'])
            self.assertNotIn('后来修改的重点', payload['messages'][0]['content'])
            self.assertEqual(payload['max_tokens'], 32768)
            return json.dumps({'choices': [{'finish_reason': 'length'}],
                'usage': {'prompt_tokens': 200, 'completion_tokens': 32768}}).encode()
        self.assertFalse(run(job.pk, transport=transport))
        job.refresh_from_db()
        self.assertEqual(job.scope['reported_tokens'], 32968)
        self.assertGreater(Decimal(job.scope['reported_cost_usd']), 0)
        self.assertFalse(hasattr(job, 'result'))

    def test_preferences_move_to_settings_and_report_has_no_hypothesis_editor(self):
        self.job()
        response = self.client.get(self.url)
        self.assertNotContains(response, 'name="claim_0"')
        self.assertContains(response, '研究过程与本次阅读证据')
        prompt_url = reverse('investment_research:prompt_settings', args=[self.dossier.pk])
        self.assertContains(self.client.get(prompt_url), '通用研究偏好')
        self.client.force_login(self.outsider.user)
        self.assertEqual(self.client.get(prompt_url).status_code, 404)

    def test_legacy_queued_search_keeps_its_original_single_pass_contract(self):
        engine = AiProvider.objects.create(name='Search', provider_type='openai_compatible',
            base_url='https://open.bigmodel.cn/api/paas/v4', model_name='glm-test',
            extra_data={'api_key_env_var': 'RESEARCH_TEST_KEY'})
        with self.captureOnCommitCallbacks(execute=False):
            job = enqueue(self.actor, self.dossier, self.provider, True, include_web=True)
        self.assertEqual(job.scope['search_provider_id'], engine.pk)
        job.scope.pop('research_pipeline')
        job.scope['search_queries'] = queries_for(self.security)
        job.prompt = SYSTEM
        job.save(update_fields=['scope', 'prompt'])
        rows = [{'title': '公开资料', 'kind': 'web_search', 'date': '2026-10-01',
                 'url': 'https://example.com/news', 'text': '公开信息摘录', 'offset': None}]
        response = json.dumps({'choices': [{'finish_reason': 'stop',
            'message': {'content': json.dumps(self.output())}}]}).encode()
        with patch('investment_research.web_research.search', return_value=(rows, [{'query': 'public query'}])):
            self.assertTrue(run(job.pk, transport=lambda *a, **k: response))
        job.refresh_from_db()
        self.assertEqual(job.scope['search_included_count'], 1)
        self.assertEqual(job.sanitized_input['evidence'][-1]['text'], '公开信息摘录')


class SearchContractTests(SimpleTestCase):
    def test_statement_excerpt_preserves_period_unit_and_quote_offsets(self):
        from .financial_excerpts import statement_excerpts
        text = ('Table of contents\nCONSOLIDATED STATEMENTS OF INCOME\nSee page 45\n' + 'x' * 1900 +
            '\nCONSOLIDATED STATEMENTS OF INCOME\n(dollars in millions)\n16 Weeks Ended | 52 Weeks Ended\n'
            'August 30, 2026 | August 31, 2025\nMembership fees | 1,850 | 5,907\n'
            'Operating income | 3,801 | 11,685\nNET INCOME | 2,998 | 9,226\n' +
            '\nCONDENSED CONSOLIDATED STATEMENTS OF CASH FLOWS\n(amounts in millions)\n52 Weeks Ended\n'
            'August 30, 2026 | August 31, 2025\nNet income | 9,226 | 8,099\n'
            'Net cash provided by operating activities | 15,825 | 13,335\n')
        excerpts = statement_excerpts(text)
        self.assertEqual(len(excerpts), 2)
        for quote, start in excerpts:
            self.assertEqual(text[start:start + len(quote)], quote)
            self.assertIn('2026', quote)
            self.assertIn('millions', quote)
        self.assertIn('5,907', excerpts[0][0])
        self.assertIn('15,825', excerpts[1][0])

    def test_year_mismatch_cannot_support_question(self):
        from .thesis_analysis import _validate_output
        target = {'kind': 'question', 'index': 0, 'text': 'FY2026净利润是多少？'}
        raw = json.dumps({'assessments': [{**target, 'verdict': 'supports',
            'reason': '已有利润数据', 'evidence_ids': ['E1']}]})
        result = _validate_output(raw, [target], [{'id': 'E1', 'text': '财年截至2025-08-31净利润', 'citations': []}])
        self.assertEqual(result['assessments'][0]['verdict'], 'unknown')
        self.assertEqual(result['invalid_reference_count'], 1)

    def test_futu_excerpts_balance_statements_and_keep_period_currency(self):
        from types import SimpleNamespace
        from .preparation import _pieces
        statements = []
        for code, title, name in [(1, '利润表', '营业收入'), (2, '资产负债表', '现金'), (3, '现金流量表', '经营现金流')]:
            statements.append({'type': code, 'title': title, 'reports': [{
                'period': 'FY2025', 'period_end': '2025-08-31', 'currency': 'USD', 'standards': 'US_GAAP',
                'items': [{'field_id': i, 'name': name if i == 0 else '其他项目', 'amount': '100'} for i in range(18)]}]})
        version = SimpleNamespace(material=SimpleNamespace(kind='financials'), data={'statements': statements})
        security = SimpleNamespace(market='US', symbol='COST')
        pieces = _pieces(version, security)
        self.assertIn('利润表', pieces[0][0])
        self.assertIn('资产负债表', pieces[1][0])
        self.assertIn('现金流量表', pieces[2][0])
        self.assertIn('2025-08-31', pieces[0][0])
        self.assertIn('USD', pieces[0][0])

    def test_search_contract_filters_unsafe_links_and_never_fetches_results(self):
        from types import SimpleNamespace
        provider = SimpleNamespace(base_url='https://open.bigmodel.cn/api/paas/v4', extra_data={'api_key_env_var':'SEARCH_TEST'})
        calls = []
        def transport(request, **kwargs):
            calls.append(request)
            self.assertEqual(request.full_url, 'https://open.bigmodel.cn/api/paas/v4/web_search')
            self.assertEqual(json.loads(request.data)['search_engine'], 'search_std')
            return json.dumps({'id':'test', 'search_result':[
                {'title':'bad', 'link':'http://127.0.0.1/admin', 'content':'secret'},
                {'title':'source', 'link':'https://example.com/article', 'content':'public', 'publish_date':'2026-10-01'},
                {'title':'duplicate', 'link':'https://example.com/article', 'content':'public'}]}).encode()
        with patch.dict(os.environ, {'SEARCH_TEST':'fake'}):
            rows, receipts = search(['public company query'], provider, transport=transport)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['date'], '2026-10-01')
        self.assertEqual(receipts[0]['request_id'], 'test')
        for link in ['javascript:alert(1)', 'http://localhost', 'http://10.0.0.1/', 'https://user:password@example.com']:
            self.assertFalse(public_link(link))
