"""AI 下期复核计划须附历史原文、由成员逐项确认且绑定判断版本。"""
import gzip
import hashlib
import json
import os
from datetime import date, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from ai_analysis.models import AiAnalysisRequest, AiProvider
from family_core.models import Family, FamilyMember
from portfolio.models import Security

from .citations import resolve_quote
from .models import (
    OfficialResearchContentVersion, OfficialResearchDocument, ResearchReviewPlan,
)
from .research_ai import ResearchAiError
from .review_plan import confirm_review_plan, generate_review_plan
from .sec_content import extract_sec_html
from .services import ResearchValidationError, create_dossier, save_thesis_revision


class ReviewPlanTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        family = Family.objects.create(name="Plan family")
        user = get_user_model().objects.create_user(username="plan-owner", password="x")
        cls.actor = FamilyMember.objects.create(family=family, user=user, display_name="Owner")
        other_user = get_user_model().objects.create_user(username="plan-other", password="x")
        cls.other = FamilyMember.objects.create(family=family, user=other_user,
                                                display_name="Other")
        security = Security.objects.create(symbol="PLAN", name="Plan Inc.", market="US",
                                           asset_type=Security.TYPE_STOCK)
        cls.dossier = create_dossier(actor=cls.actor, security=security,
                                    initial_thesis="关注现金回报。", pillars=["云业务需求增长"],
                                    questions=["资本投入是否转为现金？"])
        document = OfficialResearchDocument.objects.create(
            security=security, source="sec", external_id="plan-2025", document_type="10-k",
            title="Plan 2025 10-K", period_end=date(2025, 12, 31),
            published_at=date(2026, 1, 30),
            source_url="https://www.sec.gov/Archives/edgar/data/1/plan.htm",
            metadata={"cik": "1"},
        )
        raw = ("<html><body><ix:header><xbrli:unit id='usd'><xbrli:measure>"
               "iso4217:USD</xbrli:measure></xbrli:unit>"
               "<xbrli:context id='fy'><xbrli:period><xbrli:startDate>2025-01-01"
               "</xbrli:startDate><xbrli:endDate>2025-12-31</xbrli:endDate>"
               "</xbrli:period></xbrli:context></ix:header>"
               "<h1>ITEM 8. FINANCIAL STATEMENTS AND SUPPLEMENTARY DATA</h1>"
               "<table><tr><td>Net cash provided by operating activities</td><td>"
               "<ix:nonFraction name='us-gaap:NetCashProvidedByUsedInOperatingActivities' "
               "contextRef='fy' unitRef='usd' scale='6' id='cash'>100</ix:nonFraction>"
               "</td></tr></table><p>Other text. Other text. Other text.</p>"
               "<h1>ITEM 9. CHANGES IN AND DISAGREEMENTS WITH ACCOUNTANTS</h1>"
               "</body></html>").encode()
        text = extract_sec_html(raw)
        cls.version = OfficialResearchContentVersion.objects.create(
            document=document, version_number=1, source_url=document.source_url,
            raw_sha256=hashlib.sha256(raw).hexdigest(), raw_gzip=gzip.compress(raw),
            content_text=text, content_sha256=hashlib.sha256(text.encode()).hexdigest(),
            extractor_version="test", fetched_at=timezone.now(),
        )
        cls.provider = AiProvider.objects.create(
            name="Approved", provider_type="openai_compatible", model_name="test-model",
            base_url="https://example.ai/v1", extra_data={
                "allow_research_analysis": True,
                "research_policy_version": "research-document-v1",
                "research_policy_reviewed_on": "2026-09-24",
                "api_key_env_var": "RESEARCH_PLAN_TEST_KEY",
                "research_max_input_chars": 20000,
                "research_max_output_tokens": 1500,
                "research_input_usd_per_million": "1",
                "research_output_usd_per_million": "2",
                "research_max_estimated_usd": "1",
            },
        )

    @staticmethod
    def response(evidence="E1", metric_code="operating_cash"):
        items = []
        for kind in ("pillar", "question"):
            items.append({"kind": kind, "index": 0,
                          "check": "核对下期经营现金流和业务说明。",
                          "support_signal": "现金表现和需求披露同时改善。",
                          "weakening_signal": "需求增长但现金表现转弱。",
                          "gap": "仍需下期业务披露。",
                          "metric_codes": [metric_code],
                          "evidence_ids": [evidence]})
        result = {"items": items}
        return json.dumps({"choices": [{"message": {"content": json.dumps(result)}}],
                           "usage": {"prompt_tokens": 300, "completion_tokens": 150}}).encode()

    def generate(self, *, response=None):
        with patch.dict(os.environ, {"RESEARCH_PLAN_TEST_KEY": "test-token"}):
            return generate_review_plan(
                actor=self.actor, dossier_id=self.dossier.pk, version_id=self.version.pk,
                provider_id=self.provider.pk, consent=True,
                transport=lambda request, **kwargs: response or self.response(),
                url_validator=lambda provider: "https://example.ai/v1/chat/completions",
            )

    def test_draft_is_cited_and_confirmed_plan_guides_later_review(self):
        sent = []

        def transport(request, **kwargs):
            sent.append(json.loads(request.data))
            return self.response()

        with patch.dict(os.environ, {"RESEARCH_PLAN_TEST_KEY": "test-token"}):
            analysis = generate_review_plan(
                actor=self.actor, dossier_id=self.dossier.pk, version_id=self.version.pk,
                provider_id=self.provider.pk, consent=True, transport=transport,
                url_validator=lambda provider: "https://example.ai/v1/chat/completions",
            )
        self.assertEqual(analysis.status, AiAnalysisRequest.STATUS_SUCCESS)
        self.assertIn("资本投入是否转为现金", sent[0]["messages"][1]["content"])
        item = analysis.result.result_json["items"][0]
        citation = item["citations"][0]
        self.assertIn("Net cash provided", resolve_quote(
            self.version, citation["start"], citation["end"], citation["hash"],
        )[1])
        self.assertEqual(ResearchReviewPlan.objects.count(), 0)
        self.client.force_login(self.actor.user)
        url = reverse("investment_research:review_plan", args=[self.dossier.pk])
        self.assertContains(self.client.get(url), "AI 草稿 · 待你确认")
        self.assertEqual(self.client.post(url, {"action": "confirm", "analysis_id": analysis.pk,
                                                 "selected_indexes": ["0"]}).status_code, 302)
        plan = ResearchReviewPlan.objects.get()
        self.assertEqual(len(plan.items), 1)
        self.assertEqual(plan.thesis_revision_id, self.dossier.current_revision_id)
        self.assertContains(self.client.get(url), "已确认的计划")
        future = OfficialResearchDocument.objects.create(
            security=self.dossier.security, source="sec", external_id="plan-next",
            document_type="10-q", title="Next 10-Q",
            published_at=timezone.localdate() + timedelta(days=5),
            period_end=timezone.localdate() + timedelta(days=1),
            source_url="https://www.sec.gov/Archives/edgar/data/1/next.htm",
        )
        review_url = reverse("investment_research:filing_review",
                             args=[self.dossier.pk, future.pk])
        self.assertContains(self.client.get(review_url), "你确认的下期核查计划")

    def test_consent_citation_revision_and_privacy_guards(self):
        with self.assertRaises(ResearchAiError):
            generate_review_plan(actor=self.actor, dossier_id=self.dossier.pk,
                                 version_id=self.version.pk, provider_id=self.provider.pk,
                                 consent=False)
        invalid = self.generate(response=self.response(evidence="E999"))
        self.assertEqual(invalid.result.result_json["items"][0]["citations"], [])
        self.assertIn("原文编号无效", invalid.result.result_json["items"][0]["gap"])
        invalid_metric = self.generate(response=self.response(metric_code="fabricated_ratio"))
        self.assertEqual(invalid_metric.result.result_json["items"][0]["metrics"], [])
        self.assertIn("已移除", invalid_metric.result.result_json["items"][0]["gap"])
        self.assertEqual(ResearchReviewPlan.objects.count(), 0)
        analysis = self.generate()
        with self.assertRaises(ResearchValidationError):
            confirm_review_plan(actor=self.actor, dossier_id=self.dossier.pk,
                                analysis_id=analysis.pk, selected_indexes=[0, 0])
        self.dossier.selected_metric_codes = ["depreciation"]
        self.dossier.save(update_fields=["selected_metric_codes"])
        with self.assertRaises(ResearchValidationError):
            confirm_review_plan(actor=self.actor, dossier_id=self.dossier.pk,
                                analysis_id=analysis.pk, selected_indexes=[0])
        self.dossier.selected_metric_codes = []
        self.dossier.save(update_fields=["selected_metric_codes"])
        old_revision = self.dossier.current_revision
        save_thesis_revision(actor=self.actor, dossier_id=self.dossier.pk,
                             expected_revision_id=old_revision.pk, thesis="修订判断",
                             pillars=["云业务需求增长"], questions=["现金回报如何？"],
                             change_reason="新证据")
        with self.assertRaises(ResearchValidationError):
            confirm_review_plan(actor=self.actor, dossier_id=self.dossier.pk,
                                analysis_id=analysis.pk, selected_indexes=[0])
        url = reverse("investment_research:review_plan", args=[self.dossier.pk])
        self.client.force_login(self.other.user)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.post(url, {"action": "confirm",
                                                "analysis_id": analysis.pk}).status_code, 404)
        self.actor.role = FamilyMember.ROLE_VIEWER
        self.actor.save(update_fields=["role"])
        self.client.force_login(self.actor.user)
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(self.client.post(url, {"action": "confirm",
                                                "analysis_id": analysis.pk}).status_code, 403)
