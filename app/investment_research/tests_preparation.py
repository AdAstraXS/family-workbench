import json
import os
from datetime import timedelta
from unittest.mock import patch
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from ai_analysis.models import AiAnalysisRequest
from .tests_research_ai import ResearchAiTests
from .models import CompanyMaterial, CompanyMaterialVersion, ResearchPreparation, ResearchThesisRevision
from .preparation import enqueue, run, validate, packet, confirm, TITLES
from .research_ai import ResearchAiError


class PreparationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        ResearchAiTests.setUpTestData.__func__(cls)

    def setUp(self):
        self.env = patch.dict(os.environ, {"RESEARCH_TEST_KEY": "test-token"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.urlcheck = patch("investment_research.preparation._chat_url", return_value="https://example.ai/v1/chat/completions")
        self.urlcheck.start()
        self.addCleanup(self.urlcheck.stop)
        self.client.force_login(self.user)
        self.url = reverse("investment_research:prepare", args=[self.dossier.pk])

    def output(self):
        return {"summary": "收入与现金流需要进一步核查。", "checklist": [
            {"status": "已有证据", "text": "已有官方披露摘录", "refs": ["E1"]},
            {"status": "资料缺失", "text": "缺少独立竞争资料", "refs": []}],
            "sections": [{"title": title, "understanding": "来源提到经营变化。", "refs": ["E1"],
                "uncertainty": "持续性未确认", "question": "现金流如何变化？"} for title in TITLES],
            "questions": ["现金流为何下降？"], "hypotheses": [{"claim": "现金流可能恢复",
                "refs": ["E1"], "falsifier": "现金流持续下降", "tracking": "下期经营现金流", "missing": "下期报告"}]}

    def job(self, success=True):
        with self.captureOnCommitCallbacks(execute=False):
            job = enqueue(self.actor, self.dossier, self.provider, True)
        if success:
            response = json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(self.output())}}],
                "usage": {"prompt_tokens": 300, "completion_tokens": 200}}).encode()
            run(job.pk, transport=lambda *args, **kwargs: response)
            job.refresh_from_db()
            self.assertEqual(job.status, "success", job.error_message)
        return job

    def confirmation(self, **kwargs):
        return {"revision": "0", "decision": "research", "questions": "现金流为何下降？", "use_0": "on",
            "claim_0": "现金流可能恢复", "falsifier_0": "现金流持续下降", "tracking_0": "下期经营现金流",
            "missing_0": "下期报告", "refs_0": "E1", **kwargs}

    def test_source_packet_reads_saved_versions_and_excludes_retired_metadata(self):
        for kind in ["industry", "ratings", "profile"]:
            material = CompanyMaterial.objects.create(security=self.security, key=kind, kind=kind, title=kind)
            CompanyMaterialVersion.objects.create(material=material, number=1, raw_gzip=b"x", sha256="x",
                text="not-useful-metadata", data={"payload": [{"name": "地址", "value": "公司注册地"}]}, fetched_at=timezone.now())
        p = packet(self.dossier, 12000)
        self.assertEqual(p["included_source_count"], 1)
        self.assertTrue(all(e["official_version_id"] == self.version.pk for e in p["evidence"]))
        self.assertIn(f"version={self.version.pk}", p["evidence"][0]["url"])

    def test_generation_and_get_are_private_and_do_not_create_judgments(self):
        job = self.job()
        before = AiAnalysisRequest.objects.count()
        page = self.client.get(self.url)
        self.assertContains(page, "公司初识报告")
        self.assertContains(page, "Revenue grew")
        self.assertContains(page, "不确定之处")
        self.assertContains(page, "研究分析清单")
        self.assertEqual(AiAnalysisRequest.objects.count(), before)
        self.assertEqual(ResearchThesisRevision.objects.count(), 0)
        self.client.force_login(self.outsider.user)
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.assertEqual(self.client.post(self.url, {"report": job.pk}).status_code, 404)

    def test_three_decisions_persist_and_can_resume_without_new_ai_call(self):
        job = self.job()
        for i, decision in enumerate(["watch", "pause", "research"]):
            prep = confirm(self.actor, self.dossier, job, self.confirmation(
                revision=str(i), decision=decision, reason="等下期报告"))
            self.assertEqual(prep.decision, decision)
            self.assertEqual(prep.hypotheses[0]["tracking"], "下期经营现金流")
        self.assertEqual(ResearchPreparation.objects.count(), 1)
        self.assertEqual(AiAnalysisRequest.objects.count(), 1)
        self.assertEqual(ResearchThesisRevision.objects.count(), 0)
        page = self.client.get(reverse("investment_research:company_research", args=[self.dossier.pk]))
        self.assertContains(page, "关键问题与证据")
        self.assertContains(page, "现金流可能恢复")
        form = self.client.get(reverse("investment_research:first_thesis", args=[self.dossier.pk]))
        self.assertContains(form, "现金流可能恢复")
        self.assertContains(self.client.get(reverse("investment_research:index")), "深入研究")

    def test_confirmation_validation_conflicts_and_foreign_references(self):
        job = self.job()
        for data in [self.confirmation(refs_0="E999"), self.confirmation(decision="pause"),
                     self.confirmation(falsifier_0=""), self.confirmation(decision="unknown")]:
            with self.assertRaises(ResearchAiError):
                confirm(self.actor, self.dossier, job, data)
        confirm(self.actor, self.dossier, job, self.confirmation())
        with self.assertRaises(ResearchAiError):
            confirm(self.actor, self.dossier, job, self.confirmation())
        with self.assertRaises(ResearchAiError):
            confirm(self.outsider, self.dossier, job, self.confirmation())

    def test_current_judgment_is_preserved_and_changed_revision_blocks_confirmation(self):
        from .services import save_first_thesis
        save_first_thesis(actor=self.actor, dossier_id=self.dossier.pk, thesis="我的判断", pillars=["我的假设"], questions=["我的问题"])
        self.dossier.refresh_from_db()
        job = self.job()
        self.assertEqual(job.sanitized_input["existing_judgment"]["thesis"], "我的判断")
        confirm(self.actor, self.dossier, job, self.confirmation())
        self.dossier.refresh_from_db()
        self.assertEqual(self.dossier.current_revision.thesis, "我的判断")
        self.assertEqual(self.dossier.revisions.count(), 1)
        job.scope["thesis_revision_id"] = 999
        job.save()
        with self.assertRaises(ResearchAiError):
            confirm(self.actor, self.dossier, job, self.confirmation(revision="1"))

    def test_duplicate_pending_request_reuses_job_and_run_is_at_most_once(self):
        job = self.job(False)
        self.assertEqual(self.job(False).pk, job.pk)
        response = json.dumps({"choices": [{"message": {"content": json.dumps(self.output())}}]}).encode()
        with patch("investment_research.preparation._default_transport", return_value=response) as transport:
            run(job.pk)
            run(job.pk)
        self.assertEqual(transport.call_count, 1)

    def test_failure_is_sanitized_and_expired_job_can_retry(self):
        job = self.job(False)
        def fail(*args, **kwargs):
            raise RuntimeError("secret-test-token")
        run(job.pk, transport=fail)
        job.refresh_from_db()
        self.assertEqual(job.status, "failed")
        self.assertNotIn("secret", job.error_message)
        another = self.job(False)
        AiAnalysisRequest.objects.filter(pk=another.pk).update(created_at=timezone.now() - timedelta(hours=1))
        self.assertNotEqual(self.job(False).pk, another.pk)
        another.refresh_from_db()
        self.assertEqual(another.status, "failed")

    def test_unprovided_citations_and_incomplete_report_are_rejected(self):
        result = self.output()
        result["sections"][0]["refs"] = ["E999"]
        with self.assertRaises(ResearchAiError):
            validate(json.dumps(result), [{"id": "E1"}])
        result["sections"][0]["refs"] = []
        clean = validate(json.dumps(result), [{"id": "E1"}])
        self.assertIn("资料不足", clean["sections"][0]["understanding"])
        result["sections"] = []
        with self.assertRaises(ResearchAiError):
            validate(json.dumps(result), [{"id": "E1"}])

    def test_no_consent_no_materials_or_viewer_cannot_generate(self):
        with self.assertRaises(ResearchAiError):
            enqueue(self.actor, self.dossier, self.provider, False)
        self.actor.role = "viewer"
        with self.assertRaises(ResearchAiError):
            enqueue(self.actor, self.dossier, self.provider, True)
        self.actor.role = "member"
        self.version.delete()
        with self.assertRaises(ResearchAiError):
            enqueue(self.actor, self.dossier, self.provider, True)

    def test_output_limit_and_low_cost_budget_fail_without_saving_report(self):
        job = self.job(False)
        run(job.pk, transport=lambda *a, **kw: b'{"choices":[{"finish_reason":"length"}]}')
        job.refresh_from_db()
        self.assertEqual(job.status, "failed")
        self.assertFalse(hasattr(job, "result"))
        self.provider.extra_data["research_max_estimated_usd"] = "0.000001"
        self.provider.save()
        with self.assertRaises(ResearchAiError):
            self.job(False)

    def test_post_error_retains_user_edit_and_success_redirects(self):
        job = self.job()
        data = self.confirmation(action="confirm", report=str(job.pk), questions="我编辑的问题", falsifier_0="")
        page = self.client.post(self.url, data)
        self.assertContains(page, "我编辑的问题")
        self.assertContains(page, "请填写")
        page = self.client.post(self.url, self.confirmation(action="confirm", report=str(job.pk)))
        self.assertRedirects(page, reverse("investment_research:company_research", args=[self.dossier.pk]))

    def test_same_form_cannot_charge_twice_after_completion(self):
        import uuid
        nonce = str(uuid.uuid4())
        with self.captureOnCommitCallbacks(execute=False):
            job = enqueue(self.actor, self.dossier, self.provider, True, nonce)
        AiAnalysisRequest.objects.filter(pk=job.pk).update(status="success")
        self.assertEqual(enqueue(self.actor, self.dossier, self.provider, True, nonce).pk, job.pk)
        self.assertEqual(AiAnalysisRequest.objects.count(), 1)

    def test_old_report_cannot_replace_more_recent_confirmation(self):
        older = self.job()
        newer = self.job()
        confirm(self.actor, self.dossier, newer, self.confirmation())
        with self.assertRaises(ResearchAiError):
            confirm(self.actor, self.dossier, older, self.confirmation())

    def test_packet_keeps_annual_and_quarterly_in_original_currency(self):
        material = CompanyMaterial.objects.create(security=self.security, key="facts", kind="facts", title="SEC 财务指标")
        values = [{"start":"2025-01-01","end":"2025-12-31","form":"20-F","filed":"2026-04-01","val":1000000000},
                  {"start":"2026-01-01","end":"2026-03-31","form":"10-Q","filed":"2026-05-01","val":300000000}]
        CompanyMaterialVersion.objects.create(material=material, number=1, raw_gzip=b"x", sha256="x",
            data={"facts":{"us-gaap":{"Revenues":{"units":{"CNY":values}}}}}, fetched_at=timezone.now())
        content = json.dumps(packet(self.dossier, 10000), ensure_ascii=False)
        self.assertIn("2025-12-31", content)
        self.assertIn("2026-03-31", content)
        self.assertIn("CNY", content)
        self.assertIn("单季", content)
        self.assertIn("全年", content)
