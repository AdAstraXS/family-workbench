import hashlib
import json
import os
from datetime import date
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from ai_analysis.models import AiAnalysisRequest, AiProvider
from family_core.models import Family, FamilyMember
from portfolio.models import Security

from .citations import resolve_quote
from .metric_focus import generate_metric_suggestions, save_metric_focus
from .models import OfficialResearchContentVersion, OfficialResearchDocument
from .research_ai import ResearchAiError
from .services import ResearchValidationError, create_exploration
from .tests_tenk_metrics import _version


class MetricFocusTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        family = Family.objects.create(name="Focus family")
        cls.security = Security.objects.create(symbol="TEST", name="Test", market="US", asset_type="stock")
        cls.user = get_user_model().objects.create_user(username="focus-owner", password="x")
        cls.actor = FamilyMember.objects.create(family=family, user=cls.user, display_name="Owner")
        other_user = get_user_model().objects.create_user(username="focus-other", password="x")
        cls.other = FamilyMember.objects.create(family=family, user=other_user, display_name="Other")
        cls.dossier = create_exploration(actor=cls.actor, security=cls.security)
        cls.document = OfficialResearchDocument.objects.create(
            security=cls.security, source="sec", external_id="focus-2024", document_type="10-k",
            title="Test 10-K", period_end=date(2024, 12, 31),
            source_url="https://www.sec.gov/Archives/edgar/data/1/focus.htm",
            metadata={"cik": "1"},
        )
        source = _version()
        cls.version = OfficialResearchContentVersion.objects.create(
            document=cls.document, version_number=1, source_url=cls.document.source_url,
            raw_sha256="a" * 64, raw_gzip=source.raw_gzip,
            content_text=source.content_text,
            content_sha256=hashlib.sha256(source.content_text.encode()).hexdigest(),
            extractor_version="test", fetched_at=timezone.now(),
        )
        cls.provider = AiProvider.objects.create(
            name="Approved", provider_type="openai_compatible", model_name="test-model",
            base_url="https://example.ai/v1", extra_data={
                "allow_research_analysis": True,
                "research_policy_version": "research-document-v1",
                "research_policy_reviewed_on": "2026-09-23",
                "api_key_env_var": "RESEARCH_TEST_KEY",
                "research_max_input_chars": 20000,
                "research_max_output_tokens": 1000,
                "research_input_usd_per_million": "1",
                "research_output_usd_per_million": "2",
                "research_max_estimated_usd": "1",
            },
        )

    @staticmethod
    def response(code="depreciation", evidence="E1"):
        result = {"suggestions": [{"code": code, "reason": "值得核查其变化。",
                                   "question": "变化是否持续？", "evidence_ids": [evidence]}]}
        return json.dumps({"choices": [{"message": {"content": json.dumps(result)}}],
                           "usage": {"prompt_tokens": 200, "completion_tokens": 80}}).encode()

    def test_manual_choice_hides_unselected_lease_and_is_private(self):
        self.client.force_login(self.user)
        focus_url = reverse("investment_research:metric_focus", args=[self.dossier.pk])
        metrics_url = reverse("investment_research:document_metrics",
                              args=[self.dossier.pk, self.document.pk])
        self.assertEqual(self.client.get(focus_url).status_code, 200)
        self.assertNotContains(self.client.get(metrics_url), "融资租赁负债")
        response = self.client.post(focus_url, {"action": "save", "codes": ["depreciation"]})
        self.assertEqual(response.status_code, 302)
        self.dossier.refresh_from_db()
        self.assertEqual(self.dossier.selected_metric_codes, ["depreciation"])
        self.assertContains(self.client.get(metrics_url), "固定资产折旧")
        self.assertNotContains(self.client.get(metrics_url), "融资租赁负债")
        self.client.force_login(self.other.user)
        self.assertEqual(self.client.get(focus_url).status_code, 404)
        self.assertEqual(self.client.post(focus_url, {"action": "save"}).status_code, 404)
        self.actor.role = FamilyMember.ROLE_VIEWER
        self.actor.save(update_fields=["role"])
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(focus_url).status_code, 200)
        self.assertEqual(self.client.post(focus_url, {"action": "save"}).status_code, 403)

    def test_rejects_foreign_or_duplicate_codes(self):
        for codes in (["unknown"], ["depreciation", "depreciation"], ["company_revenue"]):
            with self.assertRaises(ResearchValidationError):
                save_metric_focus(actor=self.actor, dossier_id=self.dossier.pk,
                                  version_id=self.version.pk, codes=codes)
        self.dossier.refresh_from_db()
        self.assertEqual(self.dossier.selected_metric_codes, [])

    def test_ai_suggestions_are_cited_and_never_auto_confirmed(self):
        sent = []

        def transport(request, **kwargs):
            sent.append(json.loads(request.data))
            return self.response()

        with patch.dict(os.environ, {"RESEARCH_TEST_KEY": "test-token"}):
            analysis = generate_metric_suggestions(
                actor=self.actor, dossier_id=self.dossier.pk, version_id=self.version.pk,
                provider_id=self.provider.pk, consent=True, transport=transport,
                url_validator=lambda provider: "https://example.ai/v1/chat/completions",
            )
        self.assertEqual(analysis.status, AiAnalysisRequest.STATUS_SUCCESS)
        self.assertEqual(analysis.result.result_json["suggestions"][0]["code"], "depreciation")
        citation = analysis.result.result_json["suggestions"][0]["citations"][0]
        self.assertEqual(citation["version_id"], self.version.pk)
        self.assertIn("Net cash provided", resolve_quote(
            self.version, citation["start"], citation["end"], citation["hash"],
        )[1])
        self.assertEqual(sent[0]["messages"][0]["role"], "system")
        self.dossier.refresh_from_db()
        self.assertEqual(self.dossier.selected_metric_codes, [])
        self.client.force_login(self.user)
        self.assertContains(self.client.get(reverse("investment_research:metric_focus",
                                                  args=[self.dossier.pk])), "AI 候选指标")

    def test_ai_rejects_unknown_code_and_requires_consent(self):
        with self.assertRaises(ResearchAiError):
            generate_metric_suggestions(
                actor=self.actor, dossier_id=self.dossier.pk, version_id=self.version.pk,
                provider_id=self.provider.pk, consent=False,
            )
        with patch.dict(os.environ, {"RESEARCH_TEST_KEY": "test-token"}), self.assertRaises(ResearchAiError):
            generate_metric_suggestions(
                actor=self.actor, dossier_id=self.dossier.pk, version_id=self.version.pk,
                provider_id=self.provider.pk, consent=True,
                transport=lambda request, **kwargs: self.response(code="invented"),
                url_validator=lambda provider: "https://example.ai/v1/chat/completions",
            )
        self.assertEqual(AiAnalysisRequest.objects.filter(analysis_type="metric_focus",
                                                         status=AiAnalysisRequest.STATUS_SUCCESS).count(), 0)
