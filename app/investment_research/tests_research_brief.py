"""Research brief, valuation math, and public-only next-day digest."""

import gzip
import hashlib
import json
import os
from datetime import date, timedelta
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
from .next_day_digest import (_complete_quote, _verified_summary, display_digest_result,
                              generate_next_day_digest, pending_sources)
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

    def test_brief_is_private_and_legacy_audit_url_shows_same_research(self):
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
        self.assertContains(self.client.get(reverse("investment_research:detail",
                                                    args=[self.dossier.pk])), url)
        legacy = self.client.get(url + "?mode=audit")
        self.assertContains(legacy, "公司研究简报")
        self.assertNotContains(legacy, "逐项核查")
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_edit_keeps_ai_wording_and_event_impact_visible(self):
        analysis = self.analysis()
        digest = AiAnalysisRequest.objects.create(
            family=self.family, member=self.member, provider=self.provider,
            module="investment_research", analysis_type="next_day_digest",
            prompt="test", status=AiAnalysisRequest.STATUS_SUCCESS,
            scope={"dossier_id": self.dossier.pk,
                   "thesis_revision_id": self.dossier.current_revision_id,
                   "thesis_revision_number": 1},
        )
        AiAnalysisResult.objects.create(request=digest, result_json={
            "headline": "新公告", "events": [{"title": "云业务增长",
                "impact": "核查增长能否转成现金。", "evidence": []}]})
        self.client.force_login(self.user)
        response = self.client.get(reverse("investment_research:edit",
                                           args=[self.dossier.pk]))
        self.assertContains(response, "继续核查现金。")
        self.assertContains(response, "核查增长能否转成现金。")
        self.assertContains(response, "以后分析以你保存的最新正式判断为准")
        self.assertContains(response, f"/research/{self.dossier.pk}/analysis/{analysis.pk}/")

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

    def test_first_digest_uses_latest_reporting_period_even_if_old_filing_was_fetched_later(self):
        self.analysis()
        versions = []
        for year, days_ago in ((2025, 1), (2026, 10)):
            body = f"Fiscal {year} revenue and operating cash flow improved during the year. " * 8
            document = OfficialResearchDocument.objects.create(
                security=self.security, source="sec", external_id=f"annual-{year}",
                document_type="10-k", title=f"FY{year} 10-K",
                source_url=f"https://example.com/{year}", period_end=date(year, 6, 30))
            versions.append(OfficialResearchContentVersion.objects.create(
                document=document, version_number=1, source_url=document.source_url,
                raw_sha256=hashlib.sha256(body.encode()).hexdigest(),
                raw_gzip=gzip.compress(body.encode()), content_text=body,
                content_sha256=hashlib.sha256(body.encode()).hexdigest(),
                extractor_version="test", fetched_at=timezone.now() - timedelta(days=days_ago)))
        self.assertEqual([item.pk for item in pending_sources(self.dossier)], [versions[1].pk])
        prior = AiAnalysisRequest.objects.create(
            family=self.family, member=self.member, provider=self.provider,
            module="investment_research", analysis_type="next_day_digest", prompt="old",
            status=AiAnalysisRequest.STATUS_SUCCESS,
            scope={"dossier_id": self.dossier.pk, "initial_digest": True,
                   "prompt_version": "research-event-digest-v2",
                   "sources": [{"version_id": versions[0].pk}]})
        self.assertEqual([item.pk for item in pending_sources(self.dossier)], [versions[1].pk])

    def test_event_excerpt_and_numeric_summary_drop_incomplete_or_unsupported_claims(self):
        self.assertEqual(_complete_quote("Revenue grew 15% during the fiscal year. More detail was trunca"),
                         "Revenue grew 15% during the fiscal year.")
        self.assertEqual(_complete_quote("Azure revenue was trunca"), "")
        evidence = {"E1": {"text": "Official release: revenue grew 15% in FY2026."}}
        self.assertEqual(_verified_summary("收入增长15%。利润增长99%。", ["E1"], evidence),
                         "收入增长15%。")
        evidence["E1"]["text"] += " Cloud revenue was 15 billion dollars."
        self.assertEqual(_verified_summary("收入增长15亿元。收入增长15%。", ["E1"], evidence),
                         "收入增长15%。")

    def test_saved_event_cannot_claim_to_answer_market_consensus_without_consensus_data(self):
        saved = {"events": [{"impact": "增长支持收入假设，并部分回应是否超越市场预期。仍需核查现金。"}]}
        shown = display_digest_result(saved)
        self.assertIn("不能判断是否超预期", shown["events"][0]["impact"])
        self.assertNotIn("部分回应", shown["events"][0]["impact"])
        self.assertIn("部分回应", saved["events"][0]["impact"])

    def test_provider_bound_consent_sends_private_thesis_once_and_can_be_revoked(self):
        self.analysis()
        body = "Official Azure revenue grew while infrastructure costs remained high. " * 8
        document = OfficialResearchDocument.objects.create(
            security=self.security, source="official_ir", external_id="private-event",
            document_type="earnings_release", title="Official release",
            source_url="https://example.com/release", published_at=timezone.now().date())
        OfficialResearchContentVersion.objects.create(
            document=document, version_number=1, source_url=document.source_url,
            raw_sha256=hashlib.sha256(body.encode()).hexdigest(),
            raw_gzip=gzip.compress(body.encode()), content_text=body,
            content_sha256=hashlib.sha256(body.encode()).hexdigest(),
            extractor_version="test", fetched_at=timezone.now())
        self.client.force_login(self.other)
        consent_url = reverse("investment_research:next_day_consent",
                              args=[self.dossier.pk])
        self.assertEqual(self.client.post(consent_url, {"action": "enable"}).status_code, 404)
        self.client.force_login(self.user)
        self.assertEqual(self.client.post(consent_url, {"action": "enable"}).status_code, 302)
        tracking_url = reverse("investment_research:next_day_tracking",
                               args=[self.dossier.pk])
        tracking_page = self.client.get(tracking_url)
        self.assertContains(tracking_page, "已开启个人判断对照")
        self.assertContains(tracking_page, "现在生成事件简报")
        self.assertContains(tracking_page, "正在整理官方资料并分析")
        with patch("investment_research.views.generate_next_day_digest", return_value=None) as run:
            self.assertEqual(self.client.post(reverse("investment_research:next_day_generate",
                                                     args=[self.dossier.pk])).status_code, 302)
            run.assert_called_once_with(self.dossier.pk)
        sent = []

        def transport(request, **kwargs):
            sent.append(json.loads(request.data)["messages"][1]["content"])
            content = json.dumps({"headline": "新披露需核查", "intro": "对照了已有判断。",
                "events": [{"title": "Azure增长", "summary": "云收入增长。",
                            "impact": "支持收入判断，但现金回报仍待验证。",
                            "boundary": "投入成本仍高。", "evidence_ids": ["E1"]}],
                "gap": ""})
            return json.dumps({"choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": 200, "completion_tokens": 100}}).encode()

        with patch.dict(os.environ, {"BRIEF_TEST_KEY": "test-token"}):
            digest = generate_next_day_digest(self.dossier.pk, transport=transport,
                url_validator=lambda provider: "https://example.ai/v1/chat/completions")
            self.assertIsNone(generate_next_day_digest(self.dossier.pk, transport=transport,
                url_validator=lambda provider: "https://example.ai/v1/chat/completions"))
        self.assertEqual(len(sent), 1)
        self.assertIn("我看好增长但需要确认现金回报", sent[0])
        self.assertIn("现金能否跟上", sent[0])
        self.assertTrue(digest.scope["private_comparison"])
        self.assertTrue(digest.sanitized_input["private_thesis_included"])
        second_provider = AiProvider.objects.create(
            name="Second model", provider_type="openai_compatible",
            model_name="second-model", base_url="https://example.ai/v1",
            extra_data=self.provider.extra_data)
        newer_analysis = self.analysis()
        newer_analysis.provider = second_provider
        newer_analysis.save(update_fields=["provider"])
        changed = body + " Additional official update. " * 8
        OfficialResearchContentVersion.objects.create(
            document=document, version_number=2, source_url=document.source_url,
            raw_sha256=hashlib.sha256(changed.encode()).hexdigest(),
            raw_gzip=gzip.compress(changed.encode()), content_text=changed,
            content_sha256=hashlib.sha256(changed.encode()).hexdigest(),
            extractor_version="test", fetched_at=timezone.now() + timedelta(seconds=1))
        with patch.dict(os.environ, {"BRIEF_TEST_KEY": "test-token"}):
            public_digest = generate_next_day_digest(self.dossier.pk, transport=transport,
                url_validator=lambda provider: "https://example.ai/v1/chat/completions")
        self.assertFalse(public_digest.scope["private_comparison"])
        self.assertNotIn("我看好增长", sent[-1])
        self.assertEqual(self.client.post(consent_url, {"action": "disable"}).status_code, 302)
        self.assertNotContains(self.client.get(reverse("investment_research:next_day_tracking",
                                                       args=[self.dossier.pk])),
                               "已开启个人判断对照")
