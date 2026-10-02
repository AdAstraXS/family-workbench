import gzip
import json
from types import SimpleNamespace
from unittest.mock import patch

from django.apps import apps
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from ai_analysis.models import AiAnalysisRequest, AiAnalysisResult
from .archive_bridge import link_sec_version, link_saved_sec_materials
from .material_store import save_material
from .models import OfficialResearchContentVersion, OfficialResearchDocument, ResearchPreparation
from .services import save_first_thesis, save_thesis_revision
from .tests_thesis_analysis import ThesisAnalysisTests
from .preparation import _narrative
from .tests_financial_overview import filing
from .financial_overview import build_financial_overview


class SimplificationTests(TestCase):
    response = staticmethod(ThesisAnalysisTests.response)
    generate = ThesisAnalysisTests.generate
    @classmethod
    def setUpTestData(cls):
        ThesisAnalysisTests.setUpTestData.__func__(cls)

    def archive(self, attachment="", text=None, key="annual"):
        filename = attachment or "report.htm"
        url = "https://www.sec.gov/Archives/edgar/data/123/000000012326000001/" + filename
        data = {"accession": "0000000123-26-000001", "document_type": "10-K",
                "filing_date": "2026-09-30", "report_date": "2026-08-31"}
        if attachment:
            data["attachment"] = attachment
            data["document_type"] = "8-K"
        return save_material(self.security, key, "sec_document", "Annual report", source_url=url,
            raw=b"<html>original source</html>", data=data, text=text or "Revenue and cash flow increased. " * 20)

    def preparation(self):
        job = AiAnalysisRequest.objects.create(family=self.actor.family, member=self.actor,
            module="investment_research", analysis_type="company_introduction", status="success",
            scope={"dossier_id": self.dossier.pk}, sanitized_input={"evidence": []})
        AiAnalysisResult.objects.create(request=job, result_json={"summary": "Existing introduction", "sections": []})
        return ResearchPreparation.objects.create(dossier=self.dossier, analysis=job,
            questions=["现金能否跟上？"], hypotheses=[{"claim": "需求会持续增长", "falsifier": "需求连续下降",
                "tracking": "季度订单与现金流", "missing": "客户资料", "refs": ["E1"]}], decision="research")

    def test_saved_original_is_shared_and_migration_is_idempotent(self):
        version, _ = self.archive()
        linked = OfficialResearchContentVersion.objects.get(document__external_id="0000000123-26-000001")
        self.assertEqual(gzip.decompress(bytes(linked.raw_gzip)), gzip.decompress(bytes(version.raw_gzip)))
        self.assertEqual(linked.content_text, version.text)
        self.assertEqual(linked.document.document_type, "10-k")
        self.assertEqual(linked.document.period_end.isoformat(), "2026-08-31")
        count = OfficialResearchContentVersion.objects.count()
        link_saved_sec_materials(apps, SimpleNamespace(connection=connection))
        self.assertEqual(OfficialResearchContentVersion.objects.count(), count)
        self.assertEqual(link_sec_version(version), (linked, False))

    def test_attachment_is_not_misclassified_as_annual_report(self):
        self.archive(attachment="exhibit.htm", key="appendix")
        self.archive(attachment="pressrelease.htm", text="Reports annual financial results for the year ended August 31, 2026. Revenue increased. " * 20, key="earnings")
        docs = OfficialResearchDocument.objects.filter(external_id__startswith="0000000123-26-000001:exhibit:")
        self.assertEqual(set(docs.values_list("document_type", flat=True)), {"other", "earnings_release"})
        self.assertEqual(docs.get(document_type="earnings_release").period_end.isoformat(), "2026-08-31")

    def test_shared_annual_original_reaches_financial_parser_with_citations(self):
        annual = filing()
        saved, _ = save_material(self.security, "sec-financial-test", "sec_document", "Synthetic annual",
            source_url="https://www.sec.gov/Archives/edgar/data/1/000000000126000001/report.htm",
            raw=gzip.decompress(annual.raw_gzip), text=annual.content_text,
            data={"accession": "0000000001-26-000001", "document_type": "10-K", "report_date": "2027-06-30"})
        linked, _ = link_sec_version(saved)
        periods, rows, problem = build_financial_overview(linked)
        self.assertIsNone(problem)
        self.assertEqual([period.year for period in periods], [2025, 2026, 2027])
        revenue = next(row for row in rows if row["code"] == "revenue")
        self.assertEqual(revenue["cells"][2]["amount"], 150)
        self.assertEqual(revenue["cells"][2]["citation"]["quote"], "Total revenue | 150")
        self.assertEqual(revenue["cells"][2]["version_id"], linked.pk)

    def test_corrupt_or_mismatched_original_stops_linking(self):
        version, _ = self.archive()
        version.sha256 = "0" * 64
        with self.assertRaisesMessage(ValueError, "校验失败"):
            link_sec_version(version)
        version.source_url = version.source_url.replace("000000012326000001", "000000012326000002")
        with self.assertRaisesMessage(ValueError, "不一致"):
            link_sec_version(version)

    def test_historical_catalogue_conflict_is_preserved_without_blocking_migration(self):
        version, _ = self.archive()
        doc = OfficialResearchDocument.objects.get(external_id="0000000123-26-000001")
        doc.source_url = "https://www.sec.gov/Archives/edgar/data/123/000000012326000001/old.htm"
        doc.save(update_fields=["source_url"])
        original_url = doc.source_url
        count = OfficialResearchContentVersion.objects.count()
        with self.assertLogs("investment_research.archive_bridge", level="WARNING"):
            self.assertEqual(link_sec_version(version), (None, False))
            link_saved_sec_materials(apps, SimpleNamespace(connection=connection))
        doc.refresh_from_db()
        self.assertEqual(doc.source_url, original_url)
        self.assertEqual(OfficialResearchContentVersion.objects.count(), count)
        self.assertTrue(type(version).objects.filter(pk=version.pk).exists())

    def test_follow_puts_annual_report_before_later_imported_legal_attachments(self):
        self.archive()
        self.archive(attachment="certification.htm", key="legal-appendix")
        from .company_workspace import workspace_context
        rows = workspace_context(self.dossier, {"view": "changes"})["official_rows"]
        self.assertEqual(rows[0]["version"].document.document_type, "10-k")

    def test_conditions_freeze_with_judgment_and_modified_claim_loses_old_conditions(self):
        self.dossier.current_revision = None
        self.dossier.initial_thesis = ""
        self.dossier.save(update_fields=["current_revision", "initial_thesis"])
        self.dossier.revisions.all().delete()
        prep = self.preparation()
        revision = save_first_thesis(actor=self.actor, dossier_id=self.dossier.pk, thesis="继续核查需求",
            pillars=["需求会持续增长"], questions=prep.questions)
        self.assertEqual(revision.hypothesis_context[0]["tracking"], "季度订单与现金流")
        prep.hypotheses[0]["tracking"] = "Changed later"
        prep.save()
        copied = save_thesis_revision(actor=self.actor, dossier_id=self.dossier.pk, expected_revision_id=revision.pk,
            thesis="继续观察", pillars=revision.pillars, questions=revision.questions, change_reason="复核原有假设")
        self.assertEqual(copied.hypothesis_context, revision.hypothesis_context)
        changed = save_thesis_revision(actor=self.actor, dossier_id=self.dossier.pk, expected_revision_id=copied.pk,
            thesis="需求不确定", pillars=["不同的假设"], questions=[], change_reason="修改假设")
        self.assertEqual(changed.hypothesis_context, [])

    def test_two_daily_pages_show_initial_report_and_conditions_without_side_effects(self):
        self.dossier.current_revision = None
        self.dossier.save(update_fields=["current_revision"])
        self.preparation()
        self.archive()
        self.client.force_login(self.user)
        before = AiAnalysisRequest.objects.count()
        for route in ["company_research", "follow", "research_history"]:
            with CaptureQueriesContext(connection) as queries:
                page = self.client.get(reverse("investment_research:" + route, args=[self.dossier.pk]))
            self.assertEqual(page.status_code, 200)
            self.assertFalse(any(q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for q in queries))
            if route != "research_history":
                self.assertContains(page, "季度订单与现金流")
                self.assertContains(page, "需求连续下降")
            if route == "company_research":
                self.assertContains(page, "Existing introduction")
                stages = page.content.decode().split('aria-label="公司研究流程"')[1].split("</nav>")[0]
                self.assertEqual(stages.count("<a "), 2)
        self.assertEqual(AiAnalysisRequest.objects.count(), before)
        self.client.force_login(self.other_user)
        self.assertEqual(self.client.get(reverse("investment_research:research_history", args=[self.dossier.pk])).status_code, 404)

    def test_analysis_freezes_candidate_conditions_and_distinguishes_historical_refs(self):
        self.dossier.current_revision = None
        self.dossier.save(update_fields=["current_revision"])
        prep = self.preparation()
        sent = []
        def transport(request, **kwargs):
            sent.append(json.loads(request.data)["messages"][1]["content"])
            return self.response()
        report = self.generate(transport=transport)
        self.assertEqual(report.scope["hypothesis_context"], prep.hypotheses)
        self.assertIn("季度订单与现金流", sent[0])
        self.assertIn("历史证据编号", sent[0])
        prep.hypotheses[0]["tracking"] = "Changed later"
        prep.save()
        report.refresh_from_db()
        self.assertEqual(report.scope["hypothesis_context"][0]["tracking"], "季度订单与现金流")

    def test_evidence_tab_alias_displays_evidence(self):
        self.client.force_login(self.user)
        page = self.client.get(reverse("investment_research:company_research", args=[self.dossier.pk]), {"tab": "evidence"})
        self.assertContains(page, "按假设与来源核对依据")

    def test_legal_boilerplate_is_not_selected_as_business_evidence(self):
        legal = "Forward-looking statements about revenue, risk and customers. " * 10
        business = "We manufacture memory products for customers and invest in production capacity. " * 10
        pieces = _narrative(legal + "\n\n" + business)
        self.assertTrue(pieces)
        # Offsets still point to the exact saved text.
        text = legal + "\n\n" + business
        for quote, offset in pieces:
            self.assertEqual(text[offset:offset + len(quote)], quote)
            self.assertNotIn("Forward-looking", quote)
