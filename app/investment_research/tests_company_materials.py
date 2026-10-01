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
    def test_quarterly_separates_single_quarter_and_cumulative_without_deriving_values(self):
        from .sec_fact_reading import fact_tables
        values = [{"start": start, "end": "2026-09-30", "val": val, "form": "10-Q",
                   "filed": "2026-11-01", "accn": "q"}
                  for start, val in [("2026-07-01", 300), ("2026-04-01", 600), ("2026-01-01", 900)]]
        values += [{**values[0], "form": "10-Q/A", "filed": "2026-11-02", "val": 301},
                   {**values[0], "form": "8-K", "val": 999}]
        data = {"facts": {"us-gaap": {"Revenues": {"units": {"USD": values}}}}}
        self.assertEqual(fact_tables(data), [])
        tables = {t["title"]: t for t in fact_tables(data, "quarterly")}
        self.assertEqual(set(tables), {"单季经营数据", "半年累计经营数据", "九个月累计经营数据"})
        self.assertEqual(tables["单季经营数据"]["rows"][0]["cells"][0]["amount"], "301.00")
        self.assertEqual(tables["九个月累计经营数据"]["rows"][0]["cells"][0]["amount"], "900.00")

    def test_sec_tables_align_years_and_keep_units_separate(self):
        from .sec_fact_reading import fact_tables
        def annual(year, val, **extra):
            return {"start": f"{year}-01-01", "end": f"{year}-12-31", "val": val,
                    "form": "20-F", "filed": "2026-04-01", "accn": "a", **extra}
        data = {"facts": {"us-gaap": {
            "Revenues": {"label": "Revenues", "units": {"CNY": [annual(2025, 123456789000), annual(2024, 100000000000)],
                                                       "USD": [annual(2025, 800000000)]}},
            "NetIncomeLoss": {"label": "Net income", "units": {"CNY": [annual(2025, 0)]}},
            "EarningsPerShareDiluted": {"label": "EPS", "units": {"CNY/shares": [annual(2025, "1.235")]}}
        }}}
        tables = fact_tables(data)
        cny = next(t for t in tables if t["currency"] == "CNY" and t["title"] == "年度经营数据")
        self.assertEqual(cny["unit"], "亿元")
        self.assertEqual([p["year"] for p in cny["periods"]], ["2025", "2024"])
        self.assertEqual(cny["rows"][0]["cells"][0]["amount"], "1,234.57")
        self.assertEqual([c["amount"] for c in cny["rows"][1]["cells"]], ["0.00", "—"])
        eps = next(t for t in tables if t["title"] == "每股数据")
        self.assertEqual(eps["unit"], "元/股")
        self.assertEqual(eps["rows"][0]["cells"][0]["amount"], "1.24")
        self.assertTrue(any(t["currency"] == "USD" for t in tables))

    def test_profile_prioritizes_business_over_registration(self):
        from .material_reading import profile_content
        content = profile_content({"payload": [{"name": "电话", "value": "123"},
            {"name": "员工数量", "value": "35032"}, {"name": "公司简介", "value": "设计制造汽车"},
            {"name": "公司业务", "value": "汽车销售与服务"}]})
        self.assertEqual(content["narratives"][0]["title"], "公司业务")
        self.assertEqual(content["highlights"][0]["text"], "35,032 人")
        self.assertEqual(content["details"][0]["title"], "电话")

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

    def test_saved_earnings_are_visible_separately_from_annual_and_quarterly(self):
        from .sec_financial_overview import financial_overview
        text = "Reports Fiscal Fourth-Quarter and Full-Year 2026 Results. The fiscal year ended September 3, 2026. Statements (Unaudited)."
        record = {"document_type": "8-k", "filing_date": "2026-09-30", "accession": "release"}
        save_material(self.security, "cover", "sec_document", "8-K", text=text, data=record)
        exhibit, _ = save_material(self.security, "exhibit", "sec_document", "EX99", text=text,
            data={**record, "attachment": "release.htm"}, report_date="2026-09-30")
        save_material(self.security, "other", "sec_document", "press-release.htm", text="Announces a new director.", data=record)
        save_material(self.security, "annual", "sec_document", "10-K", text="Annual report", report_date="2025-08-28",
            data={"document_type": "10-k", "filing_date": "2025-10-03"})
        facts, _ = save_material(self.security, "facts", "facts", "SEC 财务指标", data={})
        overview = financial_overview(self.security)
        self.assertEqual(len(overview["releases"]), 1)
        self.assertEqual(overview["releases"][0]["version"].pk, exhibit.pk)
        self.assertEqual(overview["releases"][0]["period"], "2026-09-03")
        self.assertEqual(overview["releases"][0]["audit"], "未经审计（原文标注）")
        self.assertEqual(overview["annual"][0]["period"], "2025-08-28")
        self.client.force_login(self.actor.user)
        for url in [reverse("investment_research:materials", args=[self.dossier.pk]),
                    reverse("investment_research:material_read", args=[self.dossier.pk, facts.pk])]:
            response = self.client.get(url)
            for label in ["最新业绩公告", "年度财务", "季度财务", "全年业绩公告", "2026-09-03", "未经审计"]:
                self.assertContains(response, label)
        self.assertEqual(CompanyMaterialVersion.objects.count(), 5)

    def test_release_detection_uses_latest_version_and_current_security(self):
        from .sec_financial_overview import financial_overview
        record = {"document_type": "8-k", "filing_date": "2026-09-30"}
        save_material(self.security, "release", "sec_document", "release", text="Reports financial results", data=record)
        save_material(self.security, "release", "sec_document", "release", text="Corrected unrelated document", data=record)
        other = Security.objects.create(symbol="OTHER", name="Other", market="US", asset_type="stock")
        save_material(other, "release", "sec_document", "release", text="Reports financial results", data=record)
        self.assertEqual(financial_overview(self.security)["releases"], [])

    def test_results_for_quarter_headline_and_unknown_audit_status(self):
        from .sec_financial_overview import report_info
        version, _ = save_material(self.security, "quarter", "sec_document", "release",
            text="MICRON REPORTS RECORD RESULTS FOR THE\n\nTHIRD QUARTER OF FISCAL 2026. Quarter ended May 28, 2026.",
            data={"document_type": "8-k", "filing_date": "2026-06-24"})
        info = report_info(version)
        self.assertTrue(info["earnings"])
        self.assertEqual(info["period"], "2026-05-28")
        self.assertEqual(info["audit"], "审计状态请见原文")

    def test_annual_page_includes_announcement_values_and_fiscal_calendar_without_writing(self):
        from .tests_sec_annual_release import fixture
        data, _, _, html = fixture()
        facts, _ = save_material(self.security, "facts", "facts", "SEC 财务指标", data=data)
        release, _ = save_material(self.security, "release", "sec_document", "Annual results", raw=html.encode(),
            media_type="text/html", text="Reports full-year 2026 results for year ended December 31, 2026. Unaudited.",
            data={"document_type": "8-k", "filing_date": "2027-01-15", "accession": "a", "attachment": "earnings.htm"})
        self.client.force_login(self.actor.user)
        response = self.client.get(reverse("investment_research:material_read", args=[self.dossier.pk, facts.pk]))
        for text in ["2026 · 业绩公告", "2026-01-01", "2026-12-31", "1.20", "-0.05", "未经审计", "公司财年"]:
            self.assertContains(response, text)
        self.assertContains(response, reverse("investment_research:material_read", args=[self.dossier.pk, release.pk]))
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

    def test_retired_sources_cannot_fetch_or_count_as_research(self):
        from .material_reading import inventory
        from .company_sources import collect_futu
        for kind in ("ratings", "industry"):
            save_material(self.security, kind, kind, kind, data={"payload": {"total": 10}})
            with self.assertRaises(ValueError):
                enqueue(self.actor, self.dossier, [kind])
            with patch("investment_research.company_sources.quote_context") as fetch:
                with self.assertRaises(ValueError):
                    collect_futu(self.security, kind)
                fetch.assert_not_called()
        save_material(self.security, "profile", "profile", "公司概况", data={"payload": [{"name": "电话", "value": "123"}]})
        _, manifest = inventory(self.security)
        self.assertEqual(manifest["steps"][0]["status"], "待补充")
        self.assertEqual(manifest["steps"][1]["status"], "待补充")
        self.assertEqual(manifest["steps"][3]["status"], "待补充")
        self.client.force_login(self.actor.user)
        response = self.client.get(reverse("investment_research:materials", args=[self.dossier.pk]))
        self.assertContains(response, "已停用资料")
        self.assertNotContains(response, 'value="ratings"')
        self.assertNotContains(response, 'value="industry"')

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
