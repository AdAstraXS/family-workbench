import gzip
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.test import TestCase, SimpleTestCase, TransactionTestCase
from django.db import connection, close_old_connections
from unittest import skipUnless
from django.urls import reverse
from django.utils import timezone
from family_core.models import Family, FamilyMember
from portfolio.models import Security
from .models import CompanyMaterial, CompanyMaterialVersion, ResearchDossier
from .material_store import save_material, record_failure
from .company_identity import search_companies, choose_company
from .company_jobs import enqueue
from .providers.futu_public import enrich_names
from .providers.sec import parse_recent_filings
from .number_display import money
from .material_reading import fact_rows


class DisplayTests(SimpleTestCase):
    def test_ratings_show_source_percentage_without_rescaling(self):
        from .material_reading import reading_sections
        version = SimpleNamespace(material=SimpleNamespace(kind="ratings"),
            data={"payload": {"buy": "60.0", "hold": "30", "sell": "10", "total": 10}})
        sections = reading_sections(version)
        self.assertEqual(sections[0], {"title": "买入占比", "text": "60.00%"})
        self.assertEqual(sections[-1], {"title": "样本数量", "text": "10"})

    def test_paginated_sdk_result_preserves_cursor_without_guessing(self):
        from .company_sources import call
        context = SimpleNamespace(get_industrial_chain_list=lambda *a, **k: (0, [{"name": "汽车"}], "opaque", 12))
        self.assertEqual(call(context, "get_industrial_chain_list", "US"),
            {"items": [{"name": "汽车"}], "next_page": "opaque", "total": 12})

    def test_original_currency_and_decimal_magnitudes(self):
        self.assertEqual(money("123456789012.3", "TWD"), "1,234.57 亿新台币")
        self.assertEqual(money("1234567", "CNY"), "123.46 万元")
        self.assertEqual(money("-1234.567", "EUR"), "-1,234.57 欧元")
        self.assertEqual(money("1.2345", "USD", per_share=True), "1.23 美元/股")
        self.assertEqual(money("NaN", "CNY"), "—")
        self.assertIn("币种未标注", money("123", ""))

    def test_foreign_filings_and_amendments(self):
        forms = ["20-F", "20-F/A", "40-F", "6-K", "6-K/A", "S-8"]
        data = {"filings": {"recent": {"accessionNumber": [str(i) for i in range(6)],
            "filingDate": ["2026-01-01"] * 6, "form": forms,
            "primaryDocument": ["a.htm"] * 6, "reportDate": [""] * 6}}}
        result = parse_recent_filings(data)
        self.assertEqual([r["document_type"] for r in result], ["20-f", "20-f", "40-f", "6-k", "6-k"])

    def test_labels_require_identity_period_currency_and_values(self):
        reports = [{"period": "2025/FY", "currency": "CNY", "items": [
            {"field_id": 5000+i, "amount": str(i*100000000), "name": ""} for i in range(1, 5)]}]
        state = {"stock_info": {"stockCode": "00700", "marketLabel": "HK"}, "financial": {
            "financialSuffix": "/financials-income-statement",
            "fieldDefinitions": [{"id": str(i), "name": "项目"+str(i)} for i in range(1, 5)],
            "financialColumns": [{"label": "2025/FY", "currencyInfo": "CNY", "financialStructure": 5,
                "values": {str(i): {"value": str(i)+".00亿"} for i in range(1, 4)}}]}}
        raw = ("<script>window.__INITIAL_STATE__="+json.dumps(state)+";</script>").encode()
        evidence = enrich_names("HK.00700", 1, reports, raw)
        self.assertEqual(len(evidence), 3)
        self.assertEqual(reports[0]["items"][0]["name"], "项目1")
        self.assertEqual(reports[0]["items"][3]["name"], "")
        with self.assertRaises(ValueError):
            enrich_names("US.NIO", 1, reports, raw)
        reports[0]["currency"] = "USD"
        self.assertEqual(enrich_names("HK.00700", 1, reports, raw), [])

    def test_ifrs_preserves_unit_and_latest_filing(self):
        values = [{"start": "2025-01-01", "end": "2025-12-31", "form": "20-F",
                   "filed": "2026-03-01", "val": 100000000, "accn": "a"},
                  {"start": "2025-01-01", "end": "2025-12-31", "form": "20-F/A",
                   "filed": "2026-04-01", "val": 120000000, "accn": "b"}]
        rows = fact_rows({"facts": {"ifrs-full": {"Revenue": {"label": "Revenue", "units": {"TWD": values}}}}})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["amount"], "1.20 亿新台币")
        self.assertEqual(rows[0]["standard"], "IFRS")
        self.assertEqual(rows[0]["accession"], "b")


class MaterialTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.family = Family.objects.create(name="materials")
        cls.actor = FamilyMember.objects.create(family=cls.family, display_name="a",
            user=get_user_model().objects.create_user("material-a"))
        cls.other = FamilyMember.objects.create(family=cls.family, display_name="b",
            user=get_user_model().objects.create_user("material-b"))
        cls.security = Security.objects.create(symbol="NIO", name="蔚来", market="US", asset_type="stock")
        cls.dossier = ResearchDossier.objects.create(family=cls.family, owner=cls.actor, security=cls.security)

    def test_archive_idempotence_versions_and_failure_preserve_original(self):
        first, changed = save_material(self.security, "profile", "profile", "公司概况", data={"a": "1"})
        self.assertTrue(changed)
        second, changed = save_material(self.security, "profile", "profile", "公司概况", data={"a": "1"})
        self.assertFalse(changed)
        self.assertEqual(first.pk, second.pk)
        third, changed = save_material(self.security, "profile", "profile", "公司概况", data={"a": "2"})
        self.assertEqual(third.number, 2)
        record_failure(self.security, "profile", "profile", "失败任务的通用标题", "fail")
        first.refresh_from_db()
        self.assertEqual(first.material.title, "公司概况")
        self.assertEqual(json.loads(gzip.decompress(bytes(first.raw_gzip))), {"a": "1"})
        self.assertEqual(CompanyMaterialVersion.objects.count(), 2)

    def test_read_download_and_get_do_not_fetch_or_write(self):
        version, _ = save_material(self.security, "profile", "profile", "公司概况", data={"payload": [{"name": "业务", "value": "制造"}]})
        self.client.force_login(self.actor.user)
        with patch("investment_research.company_sources.quote_context") as fetch:
            response = self.client.get(reverse("investment_research:materials", args=[self.dossier.pk]))
            self.assertContains(response, "五步资料清单")
            self.assertEqual(CompanyMaterialVersion.objects.count(), 1)
            fetch.assert_not_called()
        url = reverse("investment_research:material_read", args=[self.dossier.pk, version.pk])
        self.assertContains(self.client.get(url), "制造")
        self.assertIn("attachment", self.client.get(url+"?download=1")["Content-Disposition"])
        self.client.force_login(self.other.user)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.get(url+"?download=1").status_code, 404)

    def test_search_requires_selection_and_filters_non_stocks(self):
        matches, _ = search_companies("OpenAI", searcher=lambda q: [
            {"code": "US.OAIW", "name": "OpenAI ETF", "sec_type": "ETF"},
            {"code": "US.NVDA", "name": "英伟达", "sec_type": "STOCK"}])
        self.assertEqual([x["code"] for x in matches], ["US.NVDA"])
        self.assertEqual(ResearchDossier.objects.count(), 1)
        matches, _ = search_companies("蔚来", searcher=lambda q: [])
        candidate = next(x for x in matches if x["code"] == "HK.09866")
        dossier = choose_company(self.actor, candidate["token"])
        self.assertEqual(dossier.security.research_identity.sec_ticker, "NIO")
        self.assertIsNone(dossier.current_revision_id)
        self.assertEqual(choose_company(self.actor, candidate["token"]).pk, dossier.pk)

    @patch("investment_research.company_jobs.launch")
    def test_duplicate_job_and_invalid_selection(self, launch):
        first = enqueue(self.actor, self.dossier, ["profile"])
        second = enqueue(self.actor, self.dossier, ["profile"])
        self.assertEqual(first.pk, second.pk)
        with self.assertRaises(ValueError):
            enqueue(self.actor, self.dossier, ["http://localhost/private"])
        with self.assertRaises(ValueError):
            enqueue(self.other, self.dossier, ["profile"])

    def test_empty_research_is_not_marked_complete(self):
        from .material_reading import inventory
        _, manifest = inventory(self.security)
        self.assertEqual(manifest["steps"][0]["status"], "待补充")
        self.assertEqual(manifest["steps"][-1]["status"], "待分析")

    def test_source_warnings_and_sec_index_headers_are_not_evidence(self):
        from .material_reading import inventory
        version, _ = save_material(self.security, "financials", "financials", "财务资料",
            data={"warnings": ["现金流量表本次获取失败，已有版本仍保留。"]})
        save_material(self.security, "sec:header", "sec_document", "索引头",
            source_url="https://www.sec.gov/Archives/edgar/data/1/1/1-index-headers.html", text="index")
        self.client.force_login(self.actor.user)
        url = reverse("investment_research:material_read", args=[self.dossier.pk, version.pk])
        self.assertContains(self.client.get(url), "现金流量表本次获取失败")
        materials, manifest = inventory(self.security)
        self.assertEqual([m.key for m in materials], ["financials"])
        self.assertFalse(any(ref["title"] == "索引头" for step in manifest["steps"] for ref in step["materials"]))


@skipUnless(connection.vendor == "postgresql", "Row locks require PostgreSQL")
class MaterialConcurrencyTests(TransactionTestCase):
    def test_parallel_identical_archives_have_one_version(self):
        from concurrent.futures import ThreadPoolExecutor
        security = Security.objects.create(symbol="ARCHIVE", market="US", name="archive", asset_type="stock")
        def save(_):
            close_old_connections()
            try:
                return save_material(security, "profile", "profile", "profile", data={"x": "1"})[0].pk
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            ids = list(pool.map(save, range(2)))
        self.assertEqual(ids[0], ids[1])
        self.assertEqual(CompanyMaterialVersion.objects.count(), 1)

    @patch("investment_research.company_jobs.launch")
    def test_parallel_submit_launches_one_job(self, launch):
        from concurrent.futures import ThreadPoolExecutor
        family = Family.objects.create(name="concurrent")
        actor = FamilyMember.objects.create(family=family, display_name="owner",
            user=get_user_model().objects.create_user("concurrent"))
        security = Security.objects.create(symbol="JOB", market="US", name="job", asset_type="stock")
        dossier = ResearchDossier.objects.create(owner=actor, family=family, security=security)
        def submit(_):
            close_old_connections()
            try:
                return enqueue(actor, dossier, ["profile"]).pk
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            ids = list(pool.map(submit, range(2)))
        self.assertEqual(ids[0], ids[1])
        launch.assert_called_once()
