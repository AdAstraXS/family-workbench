"""Synthesis scope, provenance and member isolation."""
import gzip
import hashlib
import json
import os
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from ai_analysis.models import AiAnalysisRequest, AiProvider
from family_core.models import Family, FamilyMember
from portfolio.models import Security, SecurityMarketSnapshot

from .analysis_materials import source_preview
from .models import OfficialResearchContentVersion, OfficialResearchDocument
from .research_ai import ResearchAiError
from .services import create_dossier, save_thesis_revision
from .tests_financial_overview import filing
from .thesis_analysis import _validate_output, generate_thesis_analysis


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

    @staticmethod
    def empty_response(content="", finish_reason="stop"):
        return json.dumps({"choices": [{"message": {"content": content},
                                        "finish_reason": finish_reason}],
                           "usage": {"prompt_tokens": 500, "completion_tokens": 200}}).encode()

    def generate(self, **changes):
        arguments = {"actor": self.actor, "dossier_id": self.dossier.pk,
                     "provider_id": self.provider.pk,
                     "consent": True, "transport": lambda request, **kwargs: self.response(),
                     "url_validator": lambda provider: "https://example.ai/v1/chat/completions"}
        arguments.update(changes)
        with patch.dict(os.environ, {"SYNTHESIS_TEST_KEY": "test-token"}):
            return generate_thesis_analysis(**arguments)

    def test_prepared_sources_are_sent_once_and_result_links_saved_versions(self):
        sent = []

        def transport(request, **kwargs):
            sent.append(json.loads(request.data)["messages"][1]["content"])
            return self.response()

        analysis = self.generate(transport=transport)
        self.assertEqual(analysis.status, AiAnalysisRequest.STATUS_SUCCESS)
        self.assertEqual(len(analysis.scope["sources"]), 2)
        self.assertEqual(analysis.scope["thesis_revision_id"], self.dossier.current_revision_id)
        self.assertIn("Official report", sent[0])
        self.assertNotIn(self.versions[0].content_text, sent[0])
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

    def test_consent_and_bogus_citation_degrade_to_unknown(self):
        with self.assertRaisesMessage(ResearchAiError, "确认"):
            self.generate(consent=False)
        self.assertEqual(AiAnalysisRequest.objects.count(), 0)
        analysis = self.generate(transport=lambda request, **kwargs: self.response("E999"))
        self.assertEqual(analysis.status, AiAnalysisRequest.STATUS_SUCCESS)
        self.assertEqual(analysis.result.result_json["invalid_reference_count"], 1)
        self.assertEqual(analysis.result.result_json["assessments"][0]["verdict"], "unknown")
        self.assertFalse(analysis.result.result_json["assessments"][0]["citations"])
        self.assertFalse(analysis.result.result_json["suggested_revision"])

    def test_saved_price_and_pe_are_context_but_market_beat_needs_consensus(self):
        SecurityMarketSnapshot.objects.create(
            security=self.security, last_price=Decimal("100"),
            pe_ttm_ratio=Decimal("25"), price_as_of=timezone.now())
        sent = []

        def transport(request, **kwargs):
            sent.append(json.loads(request.data)["messages"][1]["content"])
            return self.response()

        analysis = self.generate(transport=transport)
        self.assertIn('"pe_ttm"', sent[0])
        self.assertEqual(Decimal(analysis.scope["market_context"]["price"]), Decimal("100"))
        self.assertEqual(Decimal(analysis.scope["market_context"]["pe_ttm"]), Decimal("25"))

        raw = json.dumps({"assessments": [{"kind": "question", "index": 0,
            "verdict": "supports", "reason": "Azure 增长很快。",
            "evidence_ids": ["E1"]}]}, ensure_ascii=False)
        checked = _validate_output(raw, [
            {"kind": "question", "index": 0,
             "text": "Azure AI 收入增长是否超越市场预期？"}],
            [{"id": "E1", "text": "Azure grew 40%", "citations": []}])
        self.assertEqual(checked["assessments"][0]["verdict"], "unknown")
        self.assertEqual(checked["assessments"][0]["citations"], [])
        self.assertIn("市场预期", checked["assessments"][0]["reason"])

    def test_empty_model_content_retries_once_and_counts_both_calls(self):
        sent = []

        def transport(request, **kwargs):
            sent.append(json.loads(request.data))
            return self.empty_response() if len(sent) == 1 else self.response()

        analysis = self.generate(transport=transport)
        self.assertEqual(len(sent), 2)
        self.assertEqual(analysis.sanitized_input["model_attempts"], 2)
        self.assertEqual(analysis.result.tokens_used, 1400)
        self.assertIn("非空、完整", sent[1]["messages"][0]["content"])

    def test_twice_empty_reports_specific_failure_without_raw_output(self):
        sent = []

        def transport(request, **kwargs):
            sent.append(request.data)
            return self.empty_response()

        with self.assertRaisesMessage(ResearchAiError, "空内容"):
            self.generate(transport=transport)
        self.assertEqual(len(sent), 2)
        failed = AiAnalysisRequest.objects.get()
        self.assertEqual(failed.status, AiAnalysisRequest.STATUS_FAILED)
        self.assertEqual(failed.sanitized_input["model_attempts"], 2)
        self.assertEqual(failed.sanitized_input["reported_tokens"], 1400)
        self.assertNotIn(self.dossier.current_revision.thesis, str(failed.sanitized_input))

    def test_malformed_json_retries_once(self):
        sent = []

        def transport(request, **kwargs):
            sent.append(request.data)
            return self.empty_response(content="{invalid json") if len(sent) == 1 else self.response()

        analysis = self.generate(transport=transport)
        self.assertEqual(len(sent), 2)
        self.assertEqual(analysis.sanitized_input["model_attempts"], 2)

    def test_fenced_json_is_accepted_and_length_does_not_retry(self):
        raw = json.loads(self.response())["choices"][0]["message"]["content"]
        result = _validate_output(f"```JSON\n{raw}\n```", [
            {"kind": "pillar", "index": 0, "text": "需求会持续增长"},
            {"kind": "question", "index": 0, "text": "现金能否跟上？"},
        ], [{"id": "E1", "citations": [{"version_id": 1, "document_id": 1,
                                       "start": 0, "end": 10, "hash": "test"}]}])
        self.assertEqual(result["assessments"][0]["verdict"], "supports")
        sent = []

        def transport(request, **kwargs):
            sent.append(request.data)
            return self.empty_response(content="{", finish_reason="length")

        with self.assertRaisesMessage(ResearchAiError, "长度上限"):
            self.generate(transport=transport)
        self.assertEqual(len(sent), 1)

    def test_common_evidence_id_shapes_are_normalized_and_citations_stay_bounded(self):
        targets = [{"kind": "pillar", "index": 0, "text": "需求"}]
        evidence = [{"id": f"E{number}", "citations": [{
            "version_id": number, "document_id": number, "start": 0,
            "end": 10, "hash": "test",
        }]} for number in range(1, 5)]
        for refs in ("E1", 1, {"id": "E1"}, [{"evidence_id": "E1"}], ["E1"]):
            with self.subTest(refs=refs):
                raw = json.dumps({"assessments": [{
                    "kind": "pillar", "index": 0, "verdict": "supports",
                    "reason": "材料支持需求判断。", "evidence_ids": refs,
                }], "gaps": [], "next_checks": []})
                result = _validate_output(raw, targets, evidence)
                self.assertEqual(result["assessments"][0]["verdict"], "supports")
                self.assertEqual(result["assessments"][0]["citations"][0]["id"], "E1")
        raw = json.dumps({"assessments": [{
            "kind": "pillar", "index": 0, "verdict": "supports",
            "reason": "材料支持需求判断。", "evidence_ids": "E1、E2、E3、E4",
        }]})
        result = _validate_output(raw, targets, evidence)
        self.assertEqual(len(result["assessments"][0]["citations"]), 3)

    def test_missing_or_unverifiable_model_fields_degrade_without_false_citations(self):
        targets = [{"kind": "pillar", "index": 0, "text": "需求"},
                   {"kind": "question", "index": 0, "text": "现金？"}]
        evidence = [{"id": "E1", "citations": [{"version_id": 1, "document_id": 1,
                                                  "start": 0, "end": 10, "hash": "test"}]}]
        raw = json.dumps({"assessments": [{
            "kind": "pillar", "index": 0, "verdict": "supports",
            "reason": "材料支持需求判断。", "evidence_ids": ["E1", "E999"],
        }], "gaps": "还要核查现金流", "next_checks": None,
            "suggested_revision": {"text": "不能直接采用"}})
        result = _validate_output(raw, targets, evidence)
        self.assertEqual([item["verdict"] for item in result["assessments"]],
                         ["unknown", "unknown"])
        self.assertTrue(all(not item["citations"] for item in result["assessments"]))
        self.assertEqual(result["gaps"][0], "还要核查现金流")
        self.assertEqual(result["suggested_revision"], "")

    def test_analysis_page_requires_no_source_selection(self):
        self.client.force_login(self.user)
        with patch.dict(os.environ, {"SYNTHESIS_TEST_KEY": "test-token"}):
            response = self.client.get(reverse("investment_research:thesis_analysis",
                                               args=[self.dossier.pk]))
        self.assertContains(response, "本次自动整理的资料")
        self.assertContains(response, 'id="research-synthesis-progress"')
        self.assertContains(response, "正在生成分析")
        self.assertNotContains(response, 'name="sections"')
        self.assertEqual(len(source_preview(self.dossier)), 2)

    def test_verified_three_year_financial_cells_enter_prepared_packet(self):
        sample = filing()
        document = OfficialResearchDocument.objects.create(
            security=self.security, source="sec", external_id="annual-cited",
            document_type="10-k", title="Annual report", source_url="https://www.sec.gov/annual",
            period_end=sample.document.period_end, metadata={"cik": "1"},
        )
        OfficialResearchContentVersion.objects.create(
            document=document, version_number=1, source_url=document.source_url,
            raw_sha256=hashlib.sha256(gzip.decompress(sample.raw_gzip)).hexdigest(),
            raw_gzip=sample.raw_gzip, content_text=sample.content_text,
            content_sha256=hashlib.sha256(sample.content_text.encode()).hexdigest(),
            extractor_version="test", fetched_at=timezone.now(),
        )
        sent = []
        def transport(request, **kwargs):
            sent.append(json.loads(request.data)["messages"][1]["content"])
            return self.response()
        analysis = self.generate(transport=transport)
        self.assertIn("营业收入 150.00 亿美元", sent[0])
        self.assertIn("毛利率 60.00 %", sent[0])
        self.assertEqual(analysis.scope["financial_periods"],
                         ["2025-06-30", "2026-06-30", "2027-06-30"])
        self.assertGreater(analysis.scope["financial_count"], 10)
        cite = analysis.result.result_json["assessments"][0]["citations"][0]
        self.assertEqual(cite["document_id"], document.pk)

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
