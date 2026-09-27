"""Synthesis scope, provenance and member isolation."""
import gzip
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

from .models import OfficialResearchContentVersion, OfficialResearchDocument
from .research_ai import ResearchAiError
from .services import create_dossier, save_thesis_revision
from .thesis_analysis import analysis_sections, generate_thesis_analysis


class ThesisAnalysisTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        family = Family.objects.create(name="Synthesis family")
        cls.user = get_user_model().objects.create_user(username="synthesis-owner", password="x")
        cls.actor = FamilyMember.objects.create(family=family, user=cls.user, display_name="Owner")
        cls.other_user = get_user_model().objects.create_user(username="synthesis-other", password="x")
        FamilyMember.objects.create(family=family, user=cls.other_user, display_name="Other")
        cls.security = Security.objects.create(symbol="SYN", name="Synthesis Inc.", market="US", asset_type="stock")
        cls.dossier = create_dossier(actor=cls.actor, security=cls.security,
                                     initial_thesis="需求增长但现金仍需验证。",
                                     pillars=["需求会持续增长"], questions=["现金能否跟上？"])
        cls.versions = []
        for index, source in enumerate(("sec", "official_ir"), start=1):
            document = OfficialResearchDocument.objects.create(
                security=cls.security, source=source, external_id=f"synthesis-{index}",
                document_type="10-q" if source == "sec" else "earnings_release",
                title=f"Official report {index}",
                source_url=f"https://www.sec.gov/test/{index}" if source == "sec" else f"https://example.com/report/{index}",
                published_at=date(2026, 7, index),
            )
            body = (f"Official report {index}: demand rose. Cash conversion requires checking. " * 20)
            version = OfficialResearchContentVersion.objects.create(
                document=document, version_number=1, source_url=document.source_url,
                raw_sha256=hashlib.sha256(body.encode()).hexdigest(), raw_gzip=gzip.compress(body.encode()),
                content_text=body, content_sha256=hashlib.sha256(body.encode()).hexdigest(),
                extractor_version="test", fetched_at=timezone.now(),
            )
            cls.versions.append(version)
        cls.provider = AiProvider.objects.create(
            name="Research approved", provider_type="openai_compatible", model_name="test-model",
            base_url="https://example.ai/v1", extra_data={
                "allow_research_analysis": True, "research_policy_version": "research-document-v1",
                "research_policy_reviewed_on": "2026-09-27", "api_key_env_var": "SYNTHESIS_TEST_KEY",
                "research_max_input_chars": 20000, "research_max_output_tokens": 1500,
                "research_input_usd_per_million": "1", "research_output_usd_per_million": "2",
                "research_max_estimated_usd": "1",
            },
        )

    @staticmethod
    def response(ref="E1"):
        result = {"assessments": [
            {"kind": "pillar", "index": 0, "verdict": "supports",
             "reason": "官方材料描述需求改善。", "evidence_ids": [ref]},
            {"kind": "question", "index": 0, "verdict": "unknown",
             "reason": "本次选段不足以判断现金表现。", "evidence_ids": []},
        ], "gaps": ["还需核对现金流量表。"], "next_checks": ["检查下一期现金流。"],
            "suggested_revision": "维持判断，继续核查现金。"}
        return json.dumps({"choices": [{"message": {"content": json.dumps(result)}}],
                           "usage": {"prompt_tokens": 500, "completion_tokens": 200}}).encode()

    def generate(self, **changes):
        keys = [section["key"] for section in analysis_sections(self.dossier)]
        arguments = {"actor": self.actor, "dossier_id": self.dossier.pk,
                     "section_keys": keys[:2], "provider_id": self.provider.pk,
                     "consent": True, "transport": lambda request, **kwargs: self.response(),
                     "url_validator": lambda provider: "https://example.ai/v1/chat/completions"}
        arguments.update(changes)
        with patch.dict(os.environ, {"SYNTHESIS_TEST_KEY": "test-token"}):
            return generate_thesis_analysis(**arguments)

    def test_selected_sections_are_sent_once_and_result_links_saved_versions(self):
        sent = []

        def transport(request, **kwargs):
            sent.append(json.loads(request.data)["messages"][1]["content"])
            return self.response()

        analysis = self.generate(transport=transport)
        self.assertEqual(analysis.status, AiAnalysisRequest.STATUS_SUCCESS)
        self.assertEqual(len(analysis.scope["sections"]), 2)
        self.assertEqual(analysis.scope["thesis_revision_id"], self.dossier.current_revision_id)
        self.assertIn("Official report", sent[0])
        self.assertNotIn("test-token", analysis.prompt)
        cite = analysis.result.result_json["assessments"][0]["citations"][0]
        self.assertIn(cite["version_id"], [version.pk for version in self.versions])
        cited_version = next(item for item in self.versions if item.pk == cite["version_id"])
        self.assertEqual(cite["hash"], hashlib.sha256(
            cited_version.content_text[cite["start"]:cite["end"]].encode()).hexdigest())
        self.client.force_login(self.user)
        response = self.client.get(reverse("investment_research:thesis_analysis_detail",
                                           args=[self.dossier.pk, analysis.pk]))
        self.assertContains(response, "查看原文 E1")
        self.assertContains(response, "现金表现")
        self.client.force_login(self.other_user)
        private = self.client.get(reverse("investment_research:thesis_analysis_detail",
                                          args=[self.dossier.pk, analysis.pk]))
        self.assertEqual(private.status_code, 404)

    def test_consent_and_bogus_citation_fail_closed(self):
        with self.assertRaisesMessage(ResearchAiError, "确认"):
            self.generate(consent=False)
        self.assertEqual(AiAnalysisRequest.objects.count(), 0)
        with self.assertRaisesMessage(ResearchAiError, "未提供"):
            self.generate(transport=lambda request, **kwargs: self.response("E999"))
        self.assertEqual(AiAnalysisRequest.objects.get().status, AiAnalysisRequest.STATUS_FAILED)

    def test_old_analysis_keeps_original_thesis_version(self):
        analysis = self.generate()
        save_thesis_revision(actor=self.actor, dossier_id=self.dossier.pk,
                             expected_revision_id=self.dossier.current_revision_id,
                             thesis="现在更谨慎。", pillars=["需求会持续增长"],
                             questions=["现金能否跟上？"], change_reason="新资料")
        self.client.force_login(self.user)
        response = self.client.get(reverse("investment_research:thesis_analysis_detail",
                                           args=[self.dossier.pk, analysis.pk]))
        self.assertContains(response, "旧版判断")
