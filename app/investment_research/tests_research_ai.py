import gzip
import hashlib
import json
import os
from unittest.mock import patch

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from ai_analysis.admin import AiAnalysisRequestAdmin, AiAnalysisResultAdmin
from ai_analysis.models import AiAnalysisRequest, AiAnalysisResult, AiProvider
from family_core.models import Family, FamilyMember
from portfolio.models import Security

from .models import OfficialResearchContentVersion, OfficialResearchDocument, ResearchThesisRevision
from .research_ai import ResearchAiError, available_research_providers, generate_research_draft
from .services import create_exploration, save_first_thesis


class ResearchAiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        family = Family.objects.create(name="Research family")
        outsider_family = Family.objects.create(name="Other family")
        cls.security = Security.objects.create(symbol="AAPL", name="Apple", market="US", asset_type="stock")
        cls.user = get_user_model().objects.create_user(username="draft-owner", password="x")
        cls.actor = FamilyMember.objects.create(family=family, user=cls.user, display_name="Owner")
        outsider_user = get_user_model().objects.create_user(username="draft-other", password="x")
        cls.outsider = FamilyMember.objects.create(family=outsider_family, user=outsider_user, display_name="Other")
        cls.dossier = create_exploration(actor=cls.actor, security=cls.security)
        cls.document = OfficialResearchDocument.objects.create(
            security=cls.security, source="sec", external_id="0000320193-26-000001",
            document_type="10-k", title="Apple 10-K",
            source_url="https://www.sec.gov/Archives/edgar/data/320193/000032019326000001/apple.htm",
        )
        body = "Revenue grew while operating cash flow declined. " * 20
        cls.version = OfficialResearchContentVersion.objects.create(
            document=cls.document, version_number=1, source_url=cls.document.source_url,
            raw_sha256=hashlib.sha256(body.encode()).hexdigest(), raw_gzip=gzip.compress(body.encode()),
            content_text=body, content_sha256=hashlib.sha256(body.encode()).hexdigest(),
            extractor_version="test", fetched_at=timezone.now(),
        )
        cls.provider = AiProvider.objects.create(
            name="Approved cloud", provider_type="openai_compatible", model_name="text-model",
            base_url="https://example.ai/v1", extra_data={
                "allow_research_analysis": True, "research_policy_version": "research-document-v1",
                "research_policy_reviewed_on": "2026-09-21", "api_key_env_var": "RESEARCH_TEST_KEY",
                "research_max_input_chars": 20000, "research_max_output_tokens": 1000,
                "research_input_usd_per_million": "1", "research_output_usd_per_million": "2",
                "research_max_estimated_usd": "1",
            },
        )

    @staticmethod
    def response(evidence="E1"):
        content = {"summary": "本次资料显示收入和现金流方向不同。",
                   "supports": [{"text": "收入增长", "evidence_ids": [evidence]}],
                   "weakens": [{"text": "现金流下降", "evidence_ids": [evidence]}],
                   "unknown": ["持续性未知"], "questions": ["现金流为何下降？"],
                   "suggested_revision": "继续核对现金流。"}
        return json.dumps({"choices": [{"message": {"content": json.dumps(content)}}],
                           "usage": {"prompt_tokens": 600, "completion_tokens": 100}}).encode()

    def generate(self, **overrides):
        arguments = {"actor": self.actor, "dossier_id": self.dossier.pk,
                     "version_id": self.version.pk, "provider_id": self.provider.pk,
                     "consent": True, "transport": lambda request, **kwargs: self.response(),
                     "url_validator": lambda provider: "https://example.ai/v1/chat/completions"}
        arguments.update(overrides)
        with patch.dict(os.environ, {"RESEARCH_TEST_KEY": "test-token"}):
            return generate_research_draft(**arguments)

    def test_success_has_source_links_and_does_not_write_formal_thesis(self):
        sent = []

        def transport(request, **kwargs):
            sent.append(json.loads(request.data))
            return self.response()

        analysis = self.generate(transport=transport)
        self.assertEqual(analysis.status, AiAnalysisRequest.STATUS_SUCCESS)
        self.assertEqual(analysis.scope["version_id"], self.version.pk)
        self.assertEqual(analysis.sanitized_input["source"], "sec")
        self.assertEqual(ResearchThesisRevision.objects.count(), 0)
        self.assertIsNone(self.dossier.current_revision)
        self.assertEqual(len(sent), 1)
        self.assertIn("Revenue grew", sent[0]["messages"][1]["content"])
        self.assertNotIn("Revenue grew", analysis.prompt)
        citation = analysis.result.result_json["supports"][0]["citations"][0]
        self.assertEqual(citation["version_id"], self.version.pk)
        self.client.force_login(self.user)
        page = self.client.get(reverse("investment_research:draft_detail", args=[self.dossier.pk, analysis.pk]))
        self.assertContains(page, "查看原文 E1")
        self.assertContains(page, "仅自己可见")

    def test_exploration_draft_keeps_its_original_context_after_first_thesis(self):
        analysis = self.generate()
        save_first_thesis(
            actor=self.actor, dossier_id=self.dossier.pk,
            thesis="后来形成的正式判断", pillars=[], questions=[],
        )
        self.client.force_login(self.user)
        page = self.client.get(reverse("investment_research:draft_detail", args=[self.dossier.pk, analysis.pk]))
        self.assertContains(page, "探索阶段，尚无本人正式判断")
        self.assertContains(page, "可能有利的证据")
        self.assertContains(page, "可能不利的证据")
        self.assertContains(page, "第一版判断建议草稿")
        self.assertNotContains(page, "支持当前判断的证据")
        self.assertContains(
            page, "正文获取于 " + timezone.localtime(self.version.fetched_at).strftime("%Y-%m-%d %H:%M"),
        )
        detail = self.client.get(reverse("investment_research:detail", args=[self.dossier.pk]))
        self.assertContains(detail, "最初记录（不会随当前判断修改）")

    def test_one_time_consent_and_provider_opt_in_are_required(self):
        with self.assertRaises(ResearchAiError):
            self.generate(consent=False)
        self.assertEqual(AiAnalysisRequest.objects.count(), 0)
        self.provider.extra_data["allow_research_analysis"] = False
        self.provider.save(update_fields=["extra_data"])
        self.assertEqual(available_research_providers(), [])
        with self.assertRaises(ResearchAiError):
            self.generate()
        self.assertEqual(AiAnalysisRequest.objects.count(), 0)

    def test_bad_evidence_fails_closed_and_preserves_dossier(self):
        with self.assertRaises(ResearchAiError):
            self.generate(transport=lambda request, **kwargs: self.response("E999"))
        analysis = AiAnalysisRequest.objects.get()
        self.assertEqual(analysis.status, AiAnalysisRequest.STATUS_FAILED)
        self.assertFalse(AiAnalysisResult.objects.exists())
        self.assertEqual(ResearchThesisRevision.objects.count(), 0)

    def test_draft_and_admin_privacy(self):
        analysis = self.generate()
        url = reverse("investment_research:draft_detail", args=[self.dossier.pk, analysis.pk])
        self.client.force_login(self.outsider.user)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.get(reverse("investment_research:detail", args=[self.dossier.pk])).status_code, 404)
        request = RequestFactory().get("/admin/")
        request.user = get_user_model().objects.create_superuser(username="draft-admin", password="x")
        self.assertFalse(AiAnalysisRequestAdmin(AiAnalysisRequest, admin.site).get_queryset(request).filter(pk=analysis.pk).exists())
        self.assertFalse(AiAnalysisResultAdmin(AiAnalysisResult, admin.site).get_queryset(request).filter(pk=analysis.result.pk).exists())
        self.client.force_login(request.user)
        self.assertEqual(self.client.get(reverse("admin:ai_analysis_aianalysisrequest_change", args=[analysis.pk])).status_code, 302)
        self.assertEqual(self.client.get(reverse("admin:ai_analysis_aianalysisresult_change", args=[analysis.result.pk])).status_code, 302)

    def test_post_requires_explicit_consent_and_owner(self):
        url = reverse("investment_research:generate_draft", args=[self.dossier.pk])
        self.client.force_login(self.user)
        data = {"version": self.version.pk, "provider": self.provider.pk}
        with patch("investment_research.views.generate_research_draft", side_effect=ResearchAiError("请确认本次发送范围")) as generate:
            self.assertEqual(self.client.post(url, data).status_code, 302)
            generate.assert_called_once()
            self.assertIs(generate.call_args.kwargs["consent"], False)
        self.client.force_login(self.outsider.user)
        with patch("investment_research.views.generate_research_draft") as generate:
            self.assertEqual(self.client.post(url, {**data, "one_time_consent": "yes"}).status_code, 404)
            generate.assert_not_called()

    def test_detail_get_does_not_call_model(self):
        self.client.force_login(self.user)
        with patch.dict(os.environ, {"RESEARCH_TEST_KEY": "test-token"}):
            with patch("investment_research.research_ai._default_transport") as transport:
                response = self.client.get(reverse("investment_research:detail", args=[self.dossier.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "生成一份带原文引用的草稿")
        transport.assert_not_called()
