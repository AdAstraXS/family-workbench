"""10-K 指标的金额、维度和固定原文引用拒错测试。"""
import gzip
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from family_core.models import Family, FamilyMember
from portfolio.models import Security

from .citations import resolve_quote
from .models import OfficialResearchContentVersion, OfficialResearchDocument
from .sec_content import extract_sec_html
from .services import create_dossier
from .tenk_metrics import tenk_metric_grid
from .tenk_history import fill_tenk_history


def _latest_rows(version):
    _, grid, problem = tenk_metric_grid(version)
    return [dict(code=row["code"], label=row["label"], **row["cells"][0]) for row in grid], problem


def _version(*, duplicate=False, dimensioned=False, visible=True, zero=False, wrong_unit=False):
    # 故意让同一个概念既有整公司事实，又有较大的分部事实。
    value = "—" if zero else "100"
    fmt = ' format="ixt:fixed-zero"' if zero else ""
    cash_line = (f"Net cash provided by operating activities | {value}"
                 if visible else "Operating activities were strong")
    unit = "shares" if wrong_unit else "usd"
    fact = (f'<ix:nonFraction name="us-gaap:NetCashProvidedByUsedInOperatingActivities" '
            f'contextRef="fy" unitRef="{unit}" scale="6" id="cash"{fmt}>{value}</ix:nonFraction>')
    repeat = fact.replace('id="cash"', 'id="cash-copy"') if duplicate else ""
    segment = (f'<ix:nonFraction name="us-gaap:NetCashProvidedByUsedInOperatingActivities" '
               f'contextRef="segment" unitRef="usd" scale="6" id="wrong">999</ix:nonFraction>') if dimensioned else ""
    raw = ("<html><body><ix:header>"
           "<xbrli:unit id='usd'><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>"
           "<xbrli:unit id='shares'><xbrli:measure>xbrli:shares</xbrli:measure></xbrli:unit>"
           "<xbrli:context id='fy'><xbrli:period><xbrli:startDate>2024-01-01</xbrli:startDate>"
           "<xbrli:endDate>2024-12-31</xbrli:endDate></xbrli:period></xbrli:context>"
           "<xbrli:context id='segment'><xbrli:entity><xbrli:segment>"
           "<xbrldi:explicitMember dimension='srt:ProductOrServiceAxis'>x:CloudMember</xbrldi:explicitMember>"
           "</xbrli:segment></xbrli:entity><xbrli:period><xbrli:startDate>2024-01-01</xbrli:startDate>"
           "<xbrli:endDate>2024-12-31</xbrli:endDate></xbrli:period></xbrli:context>"
           "</ix:header><h1>ITEM 8. FINANCIAL STATEMENTS AND SUPPLEMENTARY DATA</h1>"
           f"<table><tr><td>{cash_line.split(' | ')[0]}</td><td>{fact}</td></tr></table>"
           f"{repeat}{segment}<p>{'Other text ' * 100}</p>"
           "<h1>ITEM 9. CHANGES IN AND DISAGREEMENTS WITH ACCOUNTANTS</h1>"
           "</body></html>").encode()
    return SimpleNamespace(raw_gzip=gzip.compress(raw), content_text=extract_sec_html(raw),
                           document=SimpleNamespace(period_end=date(2024, 12, 31), document_type="10-k"))


class TenKMetricsTests(SimpleTestCase):
    def test_business_margin_requires_separately_cited_revenue_and_cost(self):
        for symbol, cik, label, member, cost_concept in (
            ("AAPL", "320193", "Services", "us-gaap:ServiceMember",
             "us-gaap:CostOfGoodsAndServicesSold"),
            ("TSLA", "1318605", "Energy generation and storage",
             "tsla:EnergyGenerationAndStorageMember", "us-gaap:CostOfRevenue"),
        ):
            with self.subTest(symbol=symbol):
                def build(include_cost):
                    cost_row = (f"<tr><td>{label}</td><td><ix:nonFraction name='{cost_concept}' "
                                "contextRef='business' unitRef='usd' scale='6' id='cost'>50"
                                "</ix:nonFraction></td></tr>") if include_cost else ""
                    raw = ("<html><body><ix:header><xbrli:unit id='usd'><xbrli:measure>"
                           "iso4217:USD</xbrli:measure></xbrli:unit>"
                           "<xbrli:context id='fy'><xbrli:period><xbrli:startDate>2024-01-01"
                           "</xbrli:startDate><xbrli:endDate>2024-12-31</xbrli:endDate>"
                           "</xbrli:period></xbrli:context>"
                           "<xbrli:context id='business'><xbrli:entity><xbrli:segment>"
                           "<xbrldi:explicitMember dimension='srt:ProductOrServiceAxis'>"
                           f"{member}</xbrldi:explicitMember></xbrli:segment></xbrli:entity>"
                           "<xbrli:period><xbrli:startDate>2024-01-01</xbrli:startDate>"
                           "<xbrli:endDate>2024-12-31</xbrli:endDate></xbrli:period>"
                           "</xbrli:context></ix:header>"
                           "<h1>ITEM 8. FINANCIAL STATEMENTS AND SUPPLEMENTARY DATA</h1>"
                           "<table><tr><td>Net cash provided by operating activities</td><td>"
                           "<ix:nonFraction name='us-gaap:NetCashProvidedByUsedInOperatingActivities' "
                           "contextRef='fy' unitRef='usd' scale='6' id='cash'>100</ix:nonFraction>"
                           f"</td></tr><tr><td>{label}</td><td><ix:nonFraction "
                           "name='us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax' "
                           "contextRef='business' unitRef='usd' scale='6' id='revenue'>200"
                           "</ix:nonFraction></td></tr>" + cost_row + "</table><p>" +
                           "Other text " * 80 + "</p>"
                           "<h1>ITEM 9. CHANGES IN AND DISAGREEMENTS WITH ACCOUNTANTS</h1>"
                           "</body></html>").encode()
                    document = SimpleNamespace(period_end=date(2024, 12, 31),
                                               document_type="10-k",
                                               security=SimpleNamespace(symbol=symbol),
                                               metadata={"cik": cik})
                    return SimpleNamespace(raw_gzip=gzip.compress(raw),
                                           content_text=extract_sec_html(raw), document=document)

                rows, problem = _latest_rows(build(True))
                self.assertIsNone(problem)
                by_code = {row["code"]: row for row in rows}
                self.assertEqual(by_code["company_revenue"]["fact_id"], "revenue")
                self.assertEqual(by_code["company_cost"]["fact_id"], "cost")
                self.assertNotEqual(by_code["company_revenue"]["citation"],
                                    by_code["company_cost"]["citation"])
                self.assertEqual(by_code["company_gross_profit"]["amount"], Decimal("1.5"))
                self.assertEqual(by_code["company_gross_margin"]["amount"], Decimal("75"))
                self.assertEqual([item["cell"]["fact_id"] for item in
                                  by_code["company_gross_margin"]["components"]],
                                 ["revenue", "cost"])
                missing_rows, _ = _latest_rows(build(False))
                missing = {row["code"]: row for row in missing_rows}
                self.assertNotIn("amount", missing["company_gross_profit"])
                self.assertNotIn("amount", missing["company_gross_margin"])

    def test_company_revenue_uses_exact_business_member_and_cited_item8_line(self):
        for symbol, cik, code, label, member in (
            ("AAPL", "320193", "iphone_revenue", "iPhone", "aapl:IPhoneMember"),
            ("TSLA", "1318605", "automotive_revenue", "Total automotive revenues",
             "tsla:AutomotiveRevenuesMember"),
        ):
            with self.subTest(symbol=symbol):
                def context(identifier, member_name):
                    segment = ("<xbrli:entity><xbrli:segment><xbrldi:explicitMember "
                               "dimension='srt:ProductOrServiceAxis'>" + member_name +
                               "</xbrldi:explicitMember></xbrli:segment></xbrli:entity>")
                    return (f"<xbrli:context id='{identifier}'>{segment}<xbrli:period>"
                            "<xbrli:startDate>2024-01-01</xbrli:startDate>"
                            "<xbrli:endDate>2024-12-31</xbrli:endDate>"
                            "</xbrli:period></xbrli:context>")

                def revenue(identifier, amount):
                    return ("<ix:nonFraction name='us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax' "
                            f"contextRef='{identifier}' unitRef='usd' scale='6' id='{identifier}'>{amount}"
                            "</ix:nonFraction>")

                raw = ("<html><body><ix:header><xbrli:unit id='usd'><xbrli:measure>"
                       "iso4217:USD</xbrli:measure></xbrli:unit>"
                       "<xbrli:context id='fy'><xbrli:period><xbrli:startDate>2024-01-01"
                       "</xbrli:startDate><xbrli:endDate>2024-12-31</xbrli:endDate>"
                       "</xbrli:period></xbrli:context>" + context("right", member) +
                       context("wrong", "x:OtherMember") +
                       "</ix:header><h1>ITEM 8. FINANCIAL STATEMENTS AND SUPPLEMENTARY DATA</h1>"
                       "<table><tr><td>Net cash provided by operating activities</td><td>"
                       "<ix:nonFraction name='us-gaap:NetCashProvidedByUsedInOperatingActivities' "
                       "contextRef='fy' unitRef='usd' scale='6' id='cash'>100</ix:nonFraction>"
                       f"</td></tr><tr><td>{label}</td><td>{revenue('right', '200')}</td></tr>"
                       f"<tr><td>Other revenue</td><td>{revenue('wrong', '900')}</td></tr></table>"
                       "<p>Other text " + "a " * 100 + "</p>"
                       "<h1>ITEM 9. CHANGES IN AND DISAGREEMENTS WITH ACCOUNTANTS</h1>"
                       "</body></html>").encode()
                document = SimpleNamespace(period_end=date(2024, 12, 31), document_type="10-k",
                                           security=SimpleNamespace(symbol=symbol), metadata={"cik": cik})
                version = SimpleNamespace(raw_gzip=gzip.compress(raw),
                                          content_text=extract_sec_html(raw), document=document)
                rows, problem = _latest_rows(version)
                self.assertIsNone(problem)
                selected = next(row for row in rows if row["code"] == code)
                self.assertEqual(selected["amount"], Decimal("2"))
                self.assertEqual(selected["fact_id"], "right")
                self.assertIn(label, selected["citation"]["quote"])
                document.metadata = {"cik": "1"}
                wrong_company_rows, _ = _latest_rows(version)
                self.assertNotIn(code, [row["code"] for row in wrong_company_rows])

    def test_historical_lease_uses_original_filing_and_rejects_wrong_cik(self):
        def filing(year, *, comparative=False, liability=None):
            years = (year, year - 1) if comparative else (year,)
            contexts = "".join(
                f"<xbrli:context id='fy{y}'><xbrli:period><xbrli:startDate>{y}-01-01</xbrli:startDate>"
                f"<xbrli:endDate>{y}-12-31</xbrli:endDate></xbrli:period></xbrli:context>"
                for y in years)
            contexts += (f"<xbrli:context id='at{year}'><xbrli:period><xbrli:instant>"
                         f"{year}-12-31</xbrli:instant></xbrli:period></xbrli:context>")
            cash = "".join(
                f"<tr><td>Net cash provided by operating activities</td><td>"
                f"<ix:nonFraction name='us-gaap:NetCashProvidedByUsedInOperatingActivities' "
                f"contextRef='fy{y}' unitRef='usd' scale='6' id='cash{y}'>100</ix:nonFraction>"
                f"</td></tr>" for y in years)
            lease = (f"<tr><td>Finance lease liabilities</td><td><ix:nonFraction "
                     f"name='us-gaap:FinanceLeaseLiability' contextRef='at{year}' "
                     f"unitRef='usd' scale='6' id='lease{year}'>{liability}</ix:nonFraction>"
                     f"</td></tr>" if liability is not None else "")
            raw = ("<html><body><ix:header><xbrli:unit id='usd'><xbrli:measure>"
                   "iso4217:USD</xbrli:measure></xbrli:unit>" + contexts +
                   "</ix:header><h1>ITEM 8. FINANCIAL STATEMENTS AND SUPPLEMENTARY DATA</h1>"
                   "<table>" + cash + lease + "</table><p>" + "Other text " * 100 +
                   "</p><h1>ITEM 9. CHANGES IN AND DISAGREEMENTS WITH ACCOUNTANTS</h1>"
                   "</body></html>").encode()
            document = SimpleNamespace(pk=year, period_end=date(year, 12, 31),
                                       document_type="10-k", source="sec", security_id=1,
                                       security=SimpleNamespace(symbol="TEST"), metadata={"cik": "1"})
            version = SimpleNamespace(pk=year, version_number=1, document=document,
                                      source_url=f"https://www.sec.gov/{year}.htm",
                                      raw_gzip=gzip.compress(raw), content_text=extract_sec_html(raw))
            document.content_versions = SimpleNamespace(all=lambda: [version])
            return version

        latest = filing(2025, comparative=True)
        original = filing(2024, liability=250)
        periods, rows, problem = tenk_metric_grid(latest, [original.document])
        self.assertIsNone(problem)
        self.assertEqual([p.year for p in periods], [2025, 2024])
        historical = next(r for r in rows if r["code"] == "finance_liability")["cells"][1]
        self.assertEqual(historical["amount"], Decimal("2.5"))
        self.assertEqual(historical["source_document_id"], 2024)
        self.assertEqual(historical["source_version_id"], 2024)
        self.assertEqual(historical["fact_ids"], ["lease2024"])
        original.document.metadata = {"cik": "0000000001"}
        _, rows, _ = tenk_metric_grid(latest, [original.document])
        self.assertEqual(next(r for r in rows if r["code"] == "finance_liability")["cells"][1]["amount"],
                         Decimal("2.5"))
        original.document.metadata = {"cik": "2"}
        _, rows, _ = tenk_metric_grid(latest, [original.document])
        self.assertNotIn("amount", next(r for r in rows if r["code"] == "finance_liability")["cells"][1])

    def test_selects_full_year_usd_and_undimensioned_fact_with_fixed_quote(self):
        version = _version(dimensioned=True)
        rows, problem = _latest_rows(version)
        self.assertIsNone(problem)
        cash = rows[0]
        self.assertEqual(cash["amount"], 1)
        self.assertEqual(cash["status"], "已核对")
        cited = resolve_quote(version, cash["citation"]["start"], cash["citation"]["end"],
                              cash["citation"]["hash"])
        self.assertIn("Net cash provided by operating activities | 100", cited[1])

    def test_repeated_identical_fact_is_not_summed_and_zero_is_distinct_from_absent(self):
        rows, _ = _latest_rows(_version(duplicate=True, zero=True))
        self.assertEqual(rows[0]["amount"], 0)
        self.assertEqual(rows[0]["status"], "已核对")
        self.assertNotIn("amount", rows[1])

    def test_missing_original_label_hides_amount(self):
        rows, _ = _latest_rows(_version(visible=False))
        self.assertEqual(rows[0]["status"], "原文引用待核对")
        self.assertNotIn("amount", rows[0])

    def test_wrong_unit_cannot_be_treated_as_usd(self):
        rows, _ = _latest_rows(_version(wrong_unit=True))
        self.assertEqual(rows[0]["status"], "本文件未见匹配的 XBRL 事实")
        self.assertNotIn("amount", rows[0])

    def test_three_year_grid_and_company_dimension_do_not_mix_purchase_obligations(self):
        contexts = "".join(
            f"<xbrli:context id='fy{year}'><xbrli:period><xbrli:startDate>{year}-01-01</xbrli:startDate>"
            f"<xbrli:endDate>{year}-12-31</xbrli:endDate></xbrli:period></xbrli:context>"
            for year in (2022, 2023, 2024)
        )
        cash = "".join(
            f'<td><ix:nonFraction name="us-gaap:NetCashProvidedByUsedInOperatingActivities" '
            f'contextRef="fy{year}" unitRef="usd" scale="6" id="cash{year}">{value}</ix:nonFraction></td>'
            for year, value in ((2024, 300), (2023, 250), (2022, 200))
        )
        capital = "".join(
            f'<td><ix:nonFraction name="us-gaap:PaymentsToAcquirePropertyPlantAndEquipment" '
            f'contextRef="fy{year}" unitRef="usd" scale="6" id="capex{year}">{value}</ix:nonFraction></td>'
            for year, value in ((2024, 100), (2023, 90), (2022, 80))
        )
        raw = ("<html><body><ix:header>"
               "<xbrli:unit id='usd'><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>"
               + contexts +
               "<xbrli:context id='lease'><xbrli:entity><xbrli:segment>"
               "<xbrldi:explicitMember dimension='us-gaap:UnrecordedUnconditionalPurchaseObligationByCategoryOfItemPurchasedAxis'>"
               "us-gaap:OperatingLeaseLeaseNotYetCommencedMember</xbrldi:explicitMember>"
               "</xbrli:segment></xbrli:entity><xbrli:period><xbrli:instant>2024-12-31</xbrli:instant>"
               "</xbrli:period></xbrli:context>"
               "</ix:header><h1>ITEM 8. FINANCIAL STATEMENTS AND SUPPLEMENTARY DATA</h1>"
               f"<table><tr><td>Net cash provided by operating activities</td>{cash}</tr>"
               f"<tr><td>Payments for acquisition of property</td>{capital}</tr></table>"
               "<p>Fixed payment obligations under additional leases were $"
               "<ix:nonFraction name='us-gaap:UnrecordedUnconditionalPurchaseObligationBalanceSheetAmount' "
               "contextRef='lease' unitRef='usd' scale='6' id='lease-fact'>523</ix:nonFraction> million and had not yet commenced.</p>"
               "<p>Purchase obligations were $"
               "<ix:nonFraction name='us-gaap:UnrecordedUnconditionalPurchaseObligationBalanceSheetAmount' "
               "contextRef='fy2024' unitRef='usd' scale='6' id='purchase-fact'>13,308</ix:nonFraction> million.</p>"
               "<h1>ITEM 9. CHANGES IN AND DISAGREEMENTS WITH ACCOUNTANTS</h1>"
               "</body></html>").encode()
        version = SimpleNamespace(raw_gzip=gzip.compress(raw), content_text=extract_sec_html(raw),
                                  document=SimpleNamespace(period_end=date(2024, 12, 31),
                                                           document_type="10-k",
                                                           security=SimpleNamespace(symbol="AAPL"),
                                                           metadata={"cik": "320193"}))
        periods, rows, problem = tenk_metric_grid(version)
        self.assertIsNone(problem)
        self.assertEqual([period.year for period in periods], [2024, 2023, 2022])
        self.assertEqual([cell["amount"] for cell in rows[0]["cells"]],
                         [Decimal("3"), Decimal("2.5"), Decimal("2")])
        self.assertEqual([cell["amount"] for cell in rows[2]["cells"]],
                         [Decimal("2"), Decimal("1.6"), Decimal("1.2")])
        lease = next(row for row in rows if row["code"] == "uncommenced_lease")
        self.assertEqual(lease["cells"][0]["amount"], Decimal("5.23"))
        self.assertEqual(lease["cells"][0]["fact_id"], "lease-fact")
        self.assertNotIn("amount", lease["cells"][1])
        version.document.metadata = {"cik": "999999"}
        _, wrong_company_rows, _ = tenk_metric_grid(version)
        self.assertNotIn("uncommenced_lease", [row["code"] for row in wrong_company_rows])


class TenKMetricsPageTests(TestCase):
    def test_fill_history_archives_only_needed_year_and_saves_its_body(self):
        family = Family.objects.create(name="Family")
        security = Security.objects.create(symbol="TEST", name="Test", market="US", asset_type="stock")
        user = get_user_model().objects.create_user(username="history-owner", password="x")
        owner = FamilyMember.objects.create(family=family, user=user, display_name="Owner")
        dossier = create_dossier(actor=owner, security=security, initial_thesis="Study",
                                 pillars=[], questions=[])
        current = OfficialResearchDocument.objects.create(
            security=security, source="sec", external_id="0000000001-25-000001",
            document_type="10-k", title="FY2025", period_end=date(2025, 12, 31),
            source_url="https://www.sec.gov/Archives/edgar/data/1/000000000125000001/current.htm",
            metadata={"cik": "0000000001"},
        )
        source = _version()
        version = OfficialResearchContentVersion.objects.create(
            document=current, version_number=1, source_url=current.source_url,
            raw_sha256="a" * 64, raw_gzip=source.raw_gzip,
            content_text=source.content_text, content_sha256="b" * 64,
            extractor_version="sec-html-v2", fetched_at=timezone.now(),
        )
        record = {"accession": "0000000001-24-000001", "form": "10-K",
                  "document_type": "10-k", "filing_date": date(2025, 1, 30),
                  "report_date": date(2024, 12, 31), "primary_document": "prior.htm",
                  "title": "FY2024"}
        client = SimpleNamespace(get_filings=lambda cik: [record])
        with patch("investment_research.tenk_history.tenk_metric_grid",
                   return_value=([date(2025, 12, 31), date(2024, 12, 31)], [], None)), \
             patch("investment_research.tenk_history.fetch_sec_document_content",
                   return_value=(None, True)) as fetch:
            outcome = fill_tenk_history(actor=owner, dossier=dossier, version=version, client=client)
        self.assertEqual(outcome, {"archived": 1, "saved": 1, "missing": []})
        prior = OfficialResearchDocument.objects.get(external_id=record["accession"])
        self.assertEqual(prior.period_end, date(2024, 12, 31))
        self.assertEqual(prior.metadata["cik"], "0000000001")
        self.assertEqual(fetch.call_args.kwargs["document_id"], prior.pk)
        url = reverse("investment_research:fill_document_metrics_history",
                      args=[dossier.pk, current.pk])
        self.client.force_login(user)
        self.assertEqual(self.client.get(url).status_code, 405)
        with patch("investment_research.views.fill_tenk_history",
                   return_value={"archived": 0, "saved": 0, "missing": []}) as fill:
            response = self.client.post(url, {"version": version.pk})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(fill.call_args.kwargs["version"], version)

    def test_private_page_uses_saved_version_without_writing(self):
        family = Family.objects.create(name="Family")
        security = Security.objects.create(symbol="TEST", name="Test", market="US", asset_type="stock")
        owner_user = get_user_model().objects.create_user(username="metric-owner", password="x")
        owner = FamilyMember.objects.create(family=family, user=owner_user, display_name="Owner")
        other_user = get_user_model().objects.create_user(username="metric-other", password="x")
        FamilyMember.objects.create(family=family, user=other_user, display_name="Other")
        dossier = create_dossier(actor=owner, security=security, initial_thesis="View filing",
                                 pillars=[], questions=[])
        document = OfficialResearchDocument.objects.create(
            security=security, source="sec", external_id="0000000000-24-000001",
            document_type="10-k", title="Test 10-K", period_end=date(2024, 12, 31),
            source_url="https://www.sec.gov/Archives/edgar/data/1/000000000024000001/test.htm",
            metadata={"cik": "1"},
        )
        source = _version()
        version = OfficialResearchContentVersion.objects.create(
            document=document, version_number=1, source_url=document.source_url,
            raw_sha256="a" * 64, raw_gzip=source.raw_gzip,
            content_text=source.content_text, content_sha256="b" * 64,
            extractor_version="sec-html-v2", fetched_at=timezone.now(),
        )
        url = reverse("investment_research:document_metrics", args=[dossier.pk, document.pk])
        self.client.force_login(owner_user)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "经营现金流")
        self.assertContains(response, "FY2024")
        self.assertContains(response, f"version={version.pk}&amp;start=")
        self.assertEqual(OfficialResearchContentVersion.objects.count(), 1)
        self.client.force_login(other_user)
        self.assertEqual(self.client.get(url).status_code, 404)
