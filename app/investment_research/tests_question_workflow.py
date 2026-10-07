import gzip
import json
import os
import uuid
from decimal import Decimal
from unittest.mock import patch
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from django.urls import reverse
from ai_analysis.models import AiAnalysisRequest, AiAnalysisResult
from . import tests_research_ai as fixtures, question_ai, question_workflow as workflow, preparation
from .introduction_format import TITLES, validate as validate_introduction
from .manual_materials import add
from .models import ResearchQuestionUpdate, ResearchAutoDigestConsent, ResearchThesisRevision, ResearchSupplement
from .research_ai import ResearchAiError


class QuestionWorkflowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        fixtures.ResearchAiTests.setUpTestData.__func__(cls)

    def setUp(self):
        env = patch.dict(os.environ, {'RESEARCH_TEST_KEY': 'test-token'})
        env.start()
        self.addCleanup(env.stop)
        chat = patch('investment_research.preparation._chat_url', return_value='https://example.ai/v1/chat/completions')
        chat.start()
        self.addCleanup(chat.stop)
        self.client.force_login(self.user)

    def values(self, title='现金流能否持续支持投入？'):
        return {'title': title, 'supporting_condition': '同口径现金流增长', 'reconsidering_condition': '现金流持续低于资本开支',
                'metrics': '年度经营现金流，原币种、全年口径', 'source_notes': '官方年报现金流量表'}

    def question(self):
        q = workflow.save_question(self.actor, self.dossier, self.values(), expected_list_revision=0)
        self.dossier.refresh_from_db()
        return q

    def enqueue(self, **kwargs):
        self.dossier.refresh_from_db()
        with self.captureOnCommitCallbacks(execute=False):
            return question_ai.enqueue(self.actor, self.dossier, self.provider,
                consent=True, nonce=str(uuid.uuid4()), **kwargs)

    def output(self, job, direction='unresolved'):
        return {'summary': '资料不足，仍需核实', 'updates': [{'question_id': q['question_id'], 'revision': q['revision'],
            'answer': '提供的经营现金流摘录需要完整报表核查。', 'change': '本次初次核查。',
            'direction': direction, 'refs': ['E1'], 'gap': '缺少相同报告期资本开支',
            'official_analysis': {'answer': '投研资料需完整报表核查。', 'refs': [], 'gap': '缺少资本开支'},
            'news_analysis': {'answer': '新闻尚无相关正文证据。', 'refs': [], 'gap': '缺少正文'}} for q in job.sanitized_input['questions']]}

    def response(self, content):
        return json.dumps({'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(content)}}],
                           'usage': {'prompt_tokens': 200, 'completion_tokens': 300}}).encode()

    def test_question_edits_preserve_revisions_and_invalidate_current_answer(self):
        q = self.question()
        job = self.enqueue()
        self.assertTrue(question_ai.run(job.pk, lambda *a, **kw:self.response(self.output(job))))
        self.assertIsNotNone(workflow.attach_updates([q])[0].latest_update)
        newer = workflow.save_question(self.actor, self.dossier, self.values('资本开支回报是否改善？'),
            question_id=q.pk, expected_revision=1, expected_list_revision=1)
        self.assertEqual(newer.revision, 2)
        self.assertEqual(q.revisions.count(), 2)
        self.assertIsNone(workflow.attach_updates([newer])[0].latest_update)
        self.assertTrue(newer.old_update)
        self.assertEqual(ResearchThesisRevision.objects.count(), 0)

    def test_question_history_preserves_three_sections_and_member_change_reason(self):
        q = self.question()
        job = self.enqueue()
        self.assertTrue(question_ai.run(job.pk, lambda *a, **kw: self.response(self.output(job))))
        workflow.save_question(self.actor, self.dossier,
            self.values('资本开支回报是否改善？') | {'change_note': '新年报改变了关注重点'},
            question_id=q.pk, expected_revision=1, expected_list_revision=1)
        response = self.client.get(reverse('investment_research:question_detail', args=[self.dossier.pk, q.pk]))
        for text in ['投研资料需完整报表核查。', '新闻尚无相关正文证据。', '新年报改变了关注重点', '问题已修改', '此前：']:
            self.assertContains(response, text)
        self.assertEqual(q.revisions.get(number=2).content['change_note'], '新年报改变了关注重点')

    def test_question_conflicts_duplicates_and_other_owner_blocked(self):
        q = self.question()
        for fn in [lambda:workflow.save_question(self.outsider, self.dossier, self.values()),
                   lambda:workflow.save_question(self.actor, self.dossier, self.values(), expected_list_revision=0),
                   lambda:workflow.save_question(self.actor, self.dossier, self.values(), expected_list_revision=1),
                   lambda:workflow.save_question(self.actor, self.dossier, self.values(), question_id=q.pk, expected_revision=0)]:
            with self.assertRaises(ResearchAiError):
                fn()

    def test_status_keeps_answer_and_history_without_deleting(self):
        q = self.question()
        workflow.set_question_status(self.actor, self.dossier, q.pk, 'resolved', 1)
        q.refresh_from_db()
        self.assertEqual(q.revision, 1)
        self.assertEqual(q.actions.count(), 1)
        workflow.set_question_status(self.actor, self.dossier, q.pk, 'removed', 1)
        self.assertEqual(workflow.visible_questions(self.dossier).count(), 0)
        self.assertEqual(q.revisions.count(), 1)

    def test_entering_new_flow_revokes_old_automatic_permission(self):
        consent = ResearchAutoDigestConsent.objects.create(dossier=self.dossier, provider=self.provider, authorized_by=self.actor)
        self.question()
        consent.refresh_from_db()
        self.assertIsNotNone(consent.revoked_at)

    def test_start_requires_questions_and_does_not_enable_paid_analysis(self):
        with self.assertRaises(ResearchAiError):
            workflow.start_tracking(self.actor, self.dossier, 0)
        self.question()
        workflow.start_tracking(self.actor, self.dossier, 1)
        self.dossier.refresh_from_db()
        self.assertTrue(self.dossier.is_watched)
        self.assertIsNone(question_ai.automatic_check(self.dossier))

    def test_auto_hash_no_repeat_on_success_or_failure(self):
        self.question()
        workflow.start_tracking(self.actor, self.dossier, 1)
        workflow.set_tracking_consent(self.actor, self.dossier, True, self.provider, '0.30')
        job = self.enqueue(automatic=True)
        job.status = 'failed'
        job.save(update_fields=['status'])
        repeated = self.enqueue(automatic=True)
        self.assertEqual(repeated.pk, job.pk)
        self.assertEqual(AiAnalysisRequest.objects.count(), 1)

    def test_daily_budget_unknown_usage_uses_reservation(self):
        self.question()
        workflow.start_tracking(self.actor, self.dossier, 1)
        workflow.set_tracking_consent(self.actor, self.dossier, True, self.provider, '0.0001')
        with self.assertRaisesMessage(ResearchAiError, '每日自动分析'):
            self.enqueue(automatic=True)
        self.assertEqual(AiAnalysisRequest.objects.count(), 0)

    def test_explicit_consent_required_and_get_has_no_writes(self):
        self.question()
        with self.assertRaises(ResearchAiError):
            question_ai.enqueue(self.actor, self.dossier, self.provider, nonce=str(uuid.uuid4()))
        before = AiAnalysisRequest.objects.count()
        for name in ['prepare', 'questions', 'follow', 'research_settings', 'materials', 'supplement']:
            response = self.client.get(reverse('investment_research:' + name, args=[self.dossier.pk]))
            self.assertEqual(response.status_code, 200, (name, response.status_code))
        self.assertEqual(AiAnalysisRequest.objects.count(), before)
        self.assertEqual(self.dossier.research_questions.count(), 1)

    def test_new_pages_and_originals_are_private_even_for_admin(self):
        q = self.question()
        row, _ = add(self.actor, self.dossier, {'title':'Private upload'}, SimpleUploadedFile('note.txt', b'private facts', content_type='text/plain'))
        self.outsider.user.is_superuser = True
        self.outsider.user.save()
        self.client.force_login(self.outsider.user)
        for name,args in [('questions',[self.dossier.pk]), ('follow',[self.dossier.pk]),
            ('question_detail',[self.dossier.pk,q.pk]), ('supplement_read',[self.dossier.pk,row.pk])]:
            path = reverse('investment_research:'+name, args=args)
            self.assertEqual(self.client.get(path).status_code, 404)
            self.assertEqual(self.client.post(path, {'action':'save'}).status_code, 404 if name != 'supplement_read' else 405)

    def test_changed_question_before_call_stops_without_transport(self):
        q = self.question()
        job = self.enqueue()
        workflow.save_question(self.actor, self.dossier, self.values('现金回报是否改善？'), question_id=q.pk, expected_revision=1)
        with patch('investment_research.preparation._default_transport') as transport:
            self.assertFalse(question_ai.run(job.pk))
            transport.assert_not_called()

    def test_changed_question_during_call_archives_but_does_not_publish(self):
        q = self.question()
        job = self.enqueue()
        def response(*args, **kwargs):
            workflow.save_question(self.actor, self.dossier, self.values('新的现金回报问题'), question_id=q.pk, expected_revision=1)
            return self.response(self.output(job))
        self.assertTrue(question_ai.run(job.pk, response))
        job.refresh_from_db()
        self.assertTrue(job.scope['superseded'])
        self.assertTrue(AiAnalysisResult.objects.filter(request=job).exists())
        self.assertFalse(ResearchQuestionUpdate.objects.exists())

    def test_auto_permission_revoked_before_call_blocks(self):
        self.question()
        workflow.start_tracking(self.actor, self.dossier, 1)
        workflow.set_tracking_consent(self.actor, self.dossier, True, self.provider, '0.30')
        job = self.enqueue(automatic=True)
        workflow.set_tracking_consent(self.actor, self.dossier, False, None, '0.30')
        with patch('investment_research.preparation._default_transport') as transport:
            self.assertFalse(question_ai.run(job.pk))
            transport.assert_not_called()

    def test_tracking_validates_question_versions_and_source_references(self):
        self.question()
        job = self.enqueue()
        for field,value in [('question_id', 999), ('revision', 999), ('refs', ['E999']), ('direction','unknown')]:
            data = self.output(job)
            data['updates'][0][field] = value
            with self.assertRaises(ResearchAiError):
                question_ai.validate(json.dumps(data), job.sanitized_input, question_ai.TRACK)

    def test_news_lead_cannot_support_verified_change(self):
        self.question()
        job = self.enqueue()
        job.sanitized_input['evidence'][0]['kind'] = 'news_lead'
        with self.assertRaises(ResearchAiError):
            question_ai.validate(json.dumps(self.output(job,'strengthened')), job.sanitized_input, question_ai.TRACK)

    def test_no_legacy_judgment_in_new_model_inputs(self):
        from .services import save_first_thesis
        save_first_thesis(actor=self.actor, dossier_id=self.dossier.pk, thesis='LEGACY_PRIVATE_THESIS', pillars=['OLD_PILLAR'], questions=['OLD_QUESTION'])
        self.dossier.refresh_from_db()
        self.question()
        job = self.enqueue()
        payload = json.dumps(job.sanitized_input)
        self.assertNotIn('LEGACY_PRIVATE_THESIS', payload)
        self.assertNotIn('OLD_PILLAR', payload)
        self.assertNotIn('OLD_QUESTION', payload)

    def test_five_section_introduction_strips_unrequested_questions(self):
        data = {'summary':'公司生意需要核查', 'sections':[{'title': t, 'understanding':'证据内容', 'uncertainty':'仍需核查', 'refs':['E1']} for t in TITLES],
                'questions':['不应保留'], 'hypotheses':['不应保留']}
        result = validate_introduction(json.dumps(data), [{'id':'E1'}])
        self.assertEqual([r['title'] for r in result['sections']], TITLES)
        self.assertNotIn('questions', result)
        self.assertNotIn('hypotheses', result)

    def test_new_intro_enqueue_freezes_settings_and_excludes_old_judgment(self):
        workflow.save_settings(self.actor, self.dossier, {'per_call_budget_usd':'0.30', 'daily_budget_usd':'0.30',
            'preferences':'重视现金流'}, self.provider, 0)
        with self.captureOnCommitCallbacks(execute=False):
            job = preparation.enqueue(self.actor, self.dossier, self.provider, True, str(uuid.uuid4()), simple=True, requirements='看全年数据')
        self.assertIn('重视现金流', job.prompt)
        self.assertIn('看全年数据', job.prompt)
        self.assertEqual(Decimal(job.scope['approved_max_cost_usd']), Decimal('0.30'))
        self.assertEqual(job.sanitized_input['existing_judgment'], {})

    def test_upload_duplicate_and_failed_extraction_keep_original(self):
        raw = b'unreadable binary file'
        upload = lambda:SimpleUploadedFile('bad.pdf', raw, content_type='application/pdf')
        row, created = add(self.actor, self.dossier, {'title':'File'}, upload())
        self.assertTrue(created)
        self.assertEqual(gzip.decompress(bytes(row.raw_gzip)), raw)
        self.assertEqual(row.text, '')
        self.assertIn('失败', row.extraction_note)
        duplicate, created = add(self.actor, self.dossier, {'title':'Again'}, upload())
        self.assertFalse(created)
        self.assertEqual(duplicate.pk, row.pk)
        self.assertEqual(ResearchSupplement.objects.count(), 1)

    def test_link_rejects_private_network_and_tokens_without_request(self):
        with patch('investment_research.manual_materials.public_request') as network:
            for url in ['http://127.0.0.1/', 'http://nas.local/', 'https://example.com/?api_key=secret']:
                with self.assertRaises(ResearchAiError):
                    add(self.actor, self.dossier, {'title':'File','url':url,'public_link':'yes'})
            network.assert_not_called()

    def test_upload_in_table_and_model_packet_with_private_link(self):
        row, _ = add(self.actor, self.dossier, {'title':'Own notes','period':'FY2026'},
            SimpleUploadedFile('note.txt', b'cash flow disclosure '*30, content_type='text/plain'))
        self.question()
        job = self.enqueue()
        refs = [e for e in job.sanitized_input['evidence'] if e['kind'] == 'supplement']
        self.assertEqual(refs[0]['supplement_id'], row.pk)
        self.assertIn('/supplement/', refs[0]['url'])
        page = self.client.get(reverse('investment_research:materials', args=[self.dossier.pk]), {'category':'manual'})
        self.assertContains(page, 'Own notes')
        self.assertContains(page, '<table', html=False)
        self.assertNotContains(page, 'material-grid')

    def test_settings_optimistic_revision_and_model_cap(self):
        values = {'per_call_budget_usd':'0.30','daily_budget_usd':'0.40','preferences':'cash'}
        settings = workflow.save_settings(self.actor, self.dossier, values, self.provider, 0)
        self.assertEqual(settings.revision, 1)
        with self.assertRaises(ResearchAiError):
            workflow.save_settings(self.actor, self.dossier, values, self.provider, 0)
        with self.assertRaises(ResearchAiError):
            workflow.save_settings(self.actor, self.dossier, {**values,'per_call_budget_usd':'2'}, self.provider, 1)

    def test_company_start_still_requires_search_and_market_choice(self):
        page = self.client.get(reverse('investment_research:material_start'))
        self.assertContains(page, '确认上市市场')
        self.assertContains(page, '查找公司')
        self.assertNotContains(page, '记录已有判断')
        self.assertEqual(self.dossier.research_questions.count(), 0)

    def news(self, external_id='new'):
        from investment_watch.models import NewsSource, ResearchCandidate
        from investment_watch.services import ingest
        source = NewsSource.objects.create(family=self.actor.family, key='question-news-'+external_id,
            name='Public news', url='https://example.com/rss')
        version, _ = ingest(source, external_id=external_id, title='Apple cash flow update',
            summary='Operating cash flow requires verification.', url='https://example.com/'+external_id,
            published_at=timezone.now())
        return ResearchCandidate.objects.create(dossier=self.dossier, material_version=version, reason='Public company identity')

    @override_settings(INVESTMENT_WATCH_BODY_ENABLED=True)
    def test_question_body_capture_uses_public_original_and_shared_quota(self):
        from investment_watch.body_capture import capture_question_body
        from investment_watch.models import BodyAttempt
        self.question()
        workflow.start_tracking(self.actor, self.dossier, 1)
        workflow.set_tracking_consent(self.actor, self.dossier, True, self.provider, '0.30')
        candidate = self.news()
        raw = ('<html><body><article><p>Apple operating cash flow disclosures remain subject to financial verification. ' * 10 + '</p></article></body></html>').encode()
        with patch('knowledge.web_fetch.public_request', return_value=(raw, 'text/html')) as network:
            snapshot = capture_question_body(candidate)
            self.assertEqual(snapshot.method, 'public-original')
            self.assertIn('cash flow', snapshot.text)
            self.assertEqual(network.call_count, 1)
            self.assertEqual(capture_question_body(candidate).pk, snapshot.pk)
            self.assertEqual(network.call_count, 1)
        self.assertEqual(BodyAttempt.objects.count(), 1)
        self.assertEqual(BodyAttempt.objects.first().status, 'completed')

    @override_settings(INVESTMENT_WATCH_BODY_ENABLED=True)
    def test_question_body_failed_attempt_never_retries(self):
        from investment_watch.body_capture import capture_question_body
        from investment_watch.services import WatchError
        from knowledge.web_fetch import WebCaptureError
        self.question()
        workflow.start_tracking(self.actor, self.dossier, 1)
        workflow.set_tracking_consent(self.actor, self.dossier, True, self.provider, '0.30')
        candidate = self.news()
        with patch('knowledge.web_fetch.public_request', side_effect=WebCaptureError('公开原文无法读取')) as network:
            for _ in range(2):
                with self.assertRaises(WatchError):
                    capture_question_body(candidate)
            self.assertEqual(network.call_count, 1)

    @override_settings(INVESTMENT_WATCH_BODY_ENABLED=True)
    def test_question_body_history_and_revoked_consent_do_not_fetch(self):
        from investment_watch.body_capture import capture_question_body
        from investment_watch.services import WatchError
        candidate = self.news()
        self.question()
        workflow.start_tracking(self.actor, self.dossier, 1)
        workflow.set_tracking_consent(self.actor, self.dossier, True, self.provider, '0.30')
        with patch('knowledge.web_fetch.public_request') as network:
            with self.assertRaises(WatchError):
                capture_question_body(candidate)
            workflow.set_tracking_consent(self.actor, self.dossier, False, None, '0.30')
            with self.assertRaises(WatchError):
                capture_question_body(candidate)
            network.assert_not_called()

    def test_news_source_permission_and_unread_lead_label(self):
        candidate = self.news()
        url = reverse('investment_research:question_news_source', args=[self.dossier.pk,candidate.material_version_id])
        self.assertContains(self.client.get(url), '尚无已存正文')
        self.client.force_login(self.outsider.user)
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_suggestions_remain_drafts_until_owner_selects(self):
        with self.captureOnCommitCallbacks(execute=False):
            introduction = preparation.enqueue(self.actor, self.dossier, self.provider, True, str(uuid.uuid4()), simple=True)
        intro_result = {'summary':'公司业务说明', 'sections':[{'title':t, 'understanding':'已提供资料', 'uncertainty':'仍需核查', 'refs':['E1']} for t in TITLES]}
        self.assertTrue(preparation.run(introduction.pk, lambda *a, **kw:self.response(intro_result)))
        job = self.enqueue(kind=question_ai.SUGGEST)
        output = {'summary':'待选择的问题', 'questions':[self.values(f'问题 {i}') for i in range(3)]}
        self.assertTrue(question_ai.run(job.pk, lambda *a, **kw:self.response(output)))
        self.assertFalse(self.dossier.research_questions.exists())
        page = self.client.get(reverse('investment_research:questions', args=[self.dossier.pk]))
        self.assertContains(page, '确认所选问题')
        result = self.client.post(reverse('investment_research:questions', args=[self.dossier.pk]),
            {'action':'save_suggestions','report':job.pk,'list_revision':'0','selected':['1']})
        self.assertEqual(result.status_code, 302)
        self.assertEqual(list(self.dossier.research_questions.values_list('title',flat=True)), ['问题 1'])

    def test_old_status_page_cannot_overwrite_newer_status(self):
        q = self.question()
        workflow.set_question_status(self.actor, self.dossier, q.pk, 'paused', 1, 1)
        with self.assertRaises(ResearchAiError):
            workflow.set_question_status(self.actor, self.dossier, q.pk, 'resolved', 1, 1)
        q.refresh_from_db()
        self.assertEqual(q.status, 'paused')

    def test_disabled_model_does_not_prevent_revoking_automatic_permission(self):
        self.question()
        workflow.start_tracking(self.actor, self.dossier, 1)
        consent = workflow.set_tracking_consent(self.actor, self.dossier, True, self.provider, '0.30')
        self.provider.is_active = False
        self.provider.save(update_fields=['is_active'])
        response = self.client.post(reverse('investment_research:research_settings', args=[self.dossier.pk]),
            {'revision':'0', 'provider':str(self.provider.pk), 'per_call_budget_usd':'0.30', 'daily_budget_usd':'0.30'})
        self.assertEqual(response.status_code, 302)
        consent.refresh_from_db()
        self.assertIsNotNone(consent.revoked_at)

    def test_settings_error_preserves_typed_preferences(self):
        response = self.client.post(reverse('investment_research:research_settings', args=[self.dossier.pk]),
            {'revision':'0', 'provider':str(self.provider.pk), 'per_call_budget_usd':'101',
             'daily_budget_usd':'0.30', 'preferences':'需要保留的关注点'})
        self.assertContains(response, '需要保留的关注点')
        self.assertIsNone(workflow.settings_for(self.actor))

    def test_changed_model_signature_stops_automatic_call(self):
        self.question()
        workflow.start_tracking(self.actor, self.dossier, 1)
        workflow.set_tracking_consent(self.actor, self.dossier, True, self.provider, '0.30')
        job = self.enqueue(automatic=True)
        self.provider.model_name = 'different-model'
        self.provider.save(update_fields=['model_name'])
        with patch('investment_research.question_ai._call_model') as model:
            self.assertFalse(question_ai.run(job.pk))
            model.assert_not_called()

    def test_retired_analysis_posts_cannot_generate_old_judgment(self):
        self.question()
        with patch('investment_research.views.generate_thesis_analysis') as old, patch('investment_research.views.generate_next_day_digest') as digest:
            for name in ['thesis_analysis', 'next_day_generate', 'next_day_consent', 'generate_draft', 'review_plan', 'metric_focus']:
                response = self.client.post(reverse('investment_research:'+name, args=[self.dossier.pk]))
                self.assertEqual(response.status_code, 302)
            old.assert_not_called()
            digest.assert_not_called()

    @override_settings(INVESTMENT_WATCH_BODY_ENABLED=True)
    def test_question_body_quota_is_three_for_entire_family(self):
        from investment_watch.body_capture import capture_question_body, BodyQuotaExhausted
        from investment_watch.models import BodyAttempt
        from knowledge.web_fetch import WebCaptureError
        self.question()
        workflow.start_tracking(self.actor, self.dossier, 1)
        workflow.set_tracking_consent(self.actor, self.dossier, True, self.provider, '0.30')
        candidates = [self.news(str(i)) for i in range(4)]
        with patch('knowledge.web_fetch.public_request', side_effect=WebCaptureError('暂时失败')) as network:
            for candidate in candidates[:3]:
                with self.assertRaises(Exception):
                    capture_question_body(candidate)
            with self.assertRaises(BodyQuotaExhausted):
                capture_question_body(candidates[3])
            self.assertEqual(network.call_count, 3)
        self.assertEqual(BodyAttempt.objects.count(), 3)


class QuestionConcurrencyTests(TransactionTestCase):
    def setUp(self):
        fixtures.ResearchAiTests.setUpTestData.__func__(type(self))
        QuestionWorkflowTests.setUp(self)
        workflow.save_question(self.actor, self.dossier, QuestionWorkflowTests.values(self), expected_list_revision=0)
        self.dossier.refresh_from_db()

    def concurrent(self, fn):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from django.db import close_old_connections, connection, connections
        if connection.vendor != 'postgresql':
            self.skipTest('并发事务验证需要 PostgreSQL')
        barrier = Barrier(2)
        def task():
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return fn()
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(task) for _ in range(2)]
            return [f.result(timeout=30) for f in futures]

    def test_duplicate_enqueue_creates_only_one_reserved_job(self):
        nonce = str(uuid.uuid4())
        with patch('investment_research.question_ai.launch'):
            ids = self.concurrent(lambda: question_ai.enqueue(self.actor, self.dossier, self.provider,
                consent=True, nonce=nonce).pk)
        self.assertEqual(ids[0], ids[1])
        self.assertEqual(question_ai.history(self.dossier).count(), 1)

    def test_two_workers_make_only_one_model_call(self):
        from threading import Lock
        with patch('investment_research.question_ai.launch'):
            job = question_ai.enqueue(self.actor, self.dossier, self.provider, consent=True, nonce=str(uuid.uuid4()))
        calls, guard = [], Lock()
        def transport(*args, **kwargs):
            with guard:
                calls.append(True)
            return QuestionWorkflowTests.response(self, QuestionWorkflowTests.output(self, job))
        results = self.concurrent(lambda: question_ai.run(job.pk, transport))
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(len(calls), 1)
        self.assertEqual(ResearchQuestionUpdate.objects.count(), 1)


class QuestionMigrationRetentionTests(TransactionTestCase):
    def test_migration_preserves_legacy_thesis_and_report(self):
        from django.db import connection
        from django.db.migrations.executor import MigrationExecutor
        from .services import save_first_thesis
        from .models import ResearchDossier
        fixtures.ResearchAiTests.setUpTestData.__func__(type(self))
        save_first_thesis(actor=self.actor, dossier_id=self.dossier.pk, thesis='原判断保留', pillars=[], questions=[])
        job = AiAnalysisRequest.objects.create(family=self.actor.family, member=self.actor, provider=self.provider,
            module='investment_research', analysis_type='company_introduction', status='success',
            scope={'dossier_id':self.dossier.pk})
        body = {'summary':'旧报告', 'hypotheses':[{'text':'原假设'}], 'questions':['原问题']}
        AiAnalysisResult.objects.create(request=job, result_json=body)
        executor = MigrationExecutor(connection)
        executor.migrate([('investment_research', '0018_approved_pro_report_budget')])
        try:
            executor = MigrationExecutor(connection)
            executor.migrate([('investment_research', '0020_researchautodigestconsent_provider_signature')])
            dossier = ResearchDossier.objects.get(pk=self.dossier.pk)
            self.assertFalse(dossier.question_workflow)
            self.assertEqual(dossier.question_list_revision, 0)
            self.assertEqual(dossier.current_revision.thesis, '原判断保留')
            self.assertEqual(AiAnalysisResult.objects.get(request=job).result_json, body)
            self.assertEqual(dossier.research_questions.count(), 0)
        finally:
            MigrationExecutor(connection).migrate([('investment_research', '0020_researchautodigestconsent_provider_signature')])
