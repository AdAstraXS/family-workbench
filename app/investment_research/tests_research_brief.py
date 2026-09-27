"""Research brief, valuation math, and public-only next-day digest."""

import gzip
import hashlib
import json
import os
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from ai_analysis.models import AiAnalysisRequest, AiAnalysisResult, AiProvider
from family_core.models import Family, FamilyMember
from portfolio.models import PriceSourceChoices, PricingStatusChoices, Security, SecurityMarketSnapshot

from .models import OfficialResearchContentVersion, OfficialResearchDocument
from .next_day_digest import generate_next_day_digest, pending_sources
from .services import create_dossier
from .valuation_trial import build_valuation_trial


class ResearchBriefTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.family = Family.objects.create(name="Brief family")
        cls.user = get_user_model().objects.create_user(username="brief-owner", password="x")
        cls.member = FamilyMember.objects.create(family=cls.family, user=cls.user,
                                                  display_name="Researcher")
        cls.other = get_user_model().objects.create_user(username="brief-other", password="x")
        FamilyMember.objects.create(family=cls.family, user=cls.other, display_name="Other")
        cls.security = Security.objects.create(symbol="BRF", name="Brief Inc.",
                                               market="US", asset_type="stock", currency="USD")
        cls.dossier = create_dossier(actor=cls.member, security=cls.security,
            initial_thesis="我看好增长但需要确认现金回报。",
            pillars=["收入会增长"], questions=["现金能否跟上？"])
        cls.provider = AiProvider.objects.create(
            name="Brief model", provider_type="openai_compatible", model_name="test-model",
            base_url="https://example.ai/v1", extra_data={
                "allow_research_analysis": True, "research_policy_version": "research-document-v1",
                "research_policy_reviewed_on": "2026-09-27", "api_key_env_var": "BRIEF_TEST_KEY",
                "research_max_input_chars": 20000, "research_max_output_tokens": 1500,
                "research_input_usd_per_million": "1", "research_output_usd_per_million": "2",
                "research_max_estimated_usd": "1",
            })
        cls.snapshot = SecurityMarketSnapshot.objects.create(
            security=cls.security, last_price=Decimal("100"),
            pe_ttm_ratio=Decimal("25"), price_as_of=timezone.now(),
            price_source=PriceSourceChoices.FUTU, pricing_status=PricingStatusChoices.FRESH,
        )

    def analysis(self):
        request = AiAnalysisRequest.objects.create(
            family=self.family, member=self.member, provider=self.provider,
            module="investment_research", analysis_type="thesis_synthesis",
            prompt="test", status=AiAnalysisRequest.STATUS_SUCCESS,
            scope={"dossier_id": self.dossier.pk,
                   "thesis_revision_id": self.dossier.current_revision_id,
                   "thesis_revision_number": 1, "financial_periods": ["2026-06-30"],
                   "financial_count": 10, "narrative_count": 2,
                   "valuation_basis": {"eps": "5.00", "period_end": "2026-06-30",
                       "citation": {"document_id": 1, "version_id": 1,
                                    "start": 0, "end": 10, "hash": "test"}}},
        )
        AiAnalysisResult.objects.create(request=request, result_json={
            "headline": "增长获支持，现金仍待验证", "overview": "已有披露支持部分假设。",
            "assessments": [{"kind": "pillar", "index": 0, "text": "收入会增长",
                             "verdict": "supports", "reason": "资料显示需求增长。",
                             "detail": "仍需观察利润兑现。", "boundary": "收入不等于利润。",
                             "implication": "暂不提高现金回报假设。", "citations": []}],
            "next_checks": ["检查下一期现金流。"], "gaps": [],
            "suggested_revision": "继续核查现金。"})
        return request

    def test_valuation_uses_cited_annual_eps_and_decimal_math(self):
        scope = {"valuation_basis": {"eps": "5.00", "period_end": "2026-06-30",
                 "citation": {"document_id": 1, "version_id": 1}}}
        trial = build_valuation_trial(self.security, scope,
                                      {"years": "5", "exit_pe": "20"})
        self.assertTrue(trial["available"])
        self.assertEqual(trial["starting_pe"], Decimal("20.00"))
        self.assertEqual(trial["rows"][0]["price"], Decimal("161.05"))
        self.assertEqual(trial["rows"][0]["annual_return"], Decimal("10.00"))
        self.assertEqual(trial["provider_pe_ttm"], Decimal("25"))
        implied = build_valuation_trial(self.security, {}, {"years": "1", "exit_pe": "25"})
        self.assertIn("反推", implied["basis_label"])
        self.assertEqual(implied["eps"], Decimal("4.00"))

    def test_brief_and_audit_are_private_and_show_current_quote(self):
        analysis = self.analysis()
        self.client.force_login(self.user)
        url = reverse("investment_research:thesis_analysis_detail",
                      args=[self.dossier.pk, analysis.pk])
        response = self.client.get(url)
        self.assertContains(response, "公司研究简报")
        self.assertContains(response, "$100.00")
        self.assertContains(response, "估值试算")
        self.assertContains(response, "161.05")
        self.assertContains(response, "波段辅助")
        self.assertContains(self.client.get(url + "?mode=audit"), "逐项核查")
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_next_day_digest_sends_only_public_material_and_is_idempotent(self):
        prior = self.analysis()
        AiAnalysisRequest.objects.filter(pk=prior.pk).update(
            created_at=timezone.now() - timedelta(days=1))
        body = "Official revenue growth improved while cash investment remains high. " * 8
        document = OfficialResearchDocument.objects.create(
            security=self.security, source="official_ir", external_id="brief-event",
            document_type="earnings_release", title="Official earnings release",
            source_url="https://example.com/official", published_at=timezone.now().date(),
        )
        OfficialResearchContentVersion.objects.create(
            document=document, version_number=1, source_url=document.source_url,
            raw_sha256=hashlib.sha256(body.encode()).hexdigest(),
            raw_gzip=gzip.compress(body.encode()), content_text=body,
            content_sha256=hashlib.sha256(body.encode()).hexdigest(),
            extractor_version="test", fetched_at=timezone.now() - timedelta(days=2),
        )
        self.assertEqual(len(pending_sources(self.dossier)), 1)
        sent = []

        def transport(request, **kwargs):
            sent.append(json.loads(request.data)["messages"][1]["content"])
            content = json.dumps({"headline": "新资料值得核查", "intro": "先看收入与现金。",
                "events": [{"title": "收入增长", "summary": "收入改善。",
                            "impact": "需核查利润转化。", "boundary": "仍需现金数据。",
                            "evidence_ids": ["E1"]}], "gap": ""})
            return json.dumps({"choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": 200, "completion_tokens": 100}}).encode()

        with patch.dict(os.environ, {"BRIEF_TEST_KEY": "test-token"}):
            digest = generate_next_day_digest(self.dossier.pk, transport=transport,
                url_validator=lambda provider: "https://example.ai/v1/chat/completions")
            self.assertIsNone(generate_next_day_digest(self.dossier.pk, transport=transport,
                url_validator=lambda provider: "https://example.ai/v1/chat/completions"))
        self.assertEqual(len(sent), 1)
        self.assertNotIn("我看好增长", sent[0])
        self.assertNotIn("现金能否跟上", sent[0])
        self.assertFalse(digest.sanitized_input["private_thesis_included"])
        self.assertTrue(digest.scope["initial_digest"])
        self.assertEqual(len(digest.result.result_json["events"]), 1)
        self.client.force_login(self.user)
        response = self.client.get(reverse("investment_research:next_day_tracking",
                                           args=[self.dossier.pk]))
        self.assertContains(response, "新资料值得核查")
        self.assertContains(response, "Official earnings release")
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(reverse("investment_research:next_day_tracking",
                                                args=[self.dossier.pk])).status_code, 404)
