"""The overview shows only comparable, cited facts from an archived filing."""
import gzip
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from django.test import SimpleTestCase

from .financial_overview import build_financial_overview
from .sec_content import extract_sec_html


def filing(*, conflict=False, omit_capex=False, omit_first_cash=False,
           duplicate_net_income_elsewhere=False):
    years = (2024, 2025, 2026)
    contexts = "".join(
        f"<xbrli:context id='fy{year}'><xbrli:period><xbrli:startDate>{year}-07-01</xbrli:startDate>"
        f"<xbrli:endDate>{year + 1}-06-30</xbrli:endDate></xbrli:period></xbrli:context>"
        f"<xbrli:context id='at{year}'><xbrli:period><xbrli:instant>{year + 1}-06-30"
        f"</xbrli:instant></xbrli:period></xbrli:context>"
        for year in years
    )
    tags = (
        ("revenue", "RevenueFromContractWithCustomerExcludingAssessedTax", "Total revenue", (100, 120, 150), "fy"),
        ("gross", "GrossProfit", "Gross profit", (60, 72, 90), "fy"),
        ("operating", "OperatingIncomeLoss", "Operating income", (30, 36, 45), "fy"),
        ("net", "NetIncomeLoss", "Net income", (20, 24, 30), "fy"),
        ("cash", "NetCashProvidedByUsedInOperatingActivities", "Net cash provided by operating activities", (25, 29, 32), "fy"),
        ("capex", "PaymentsToAcquirePropertyPlantAndEquipment", "Purchases of property and equipment", (5, 8, 12), "fy"),
        ("onhand", "CashAndCashEquivalentsAtCarryingValue", "Cash and cash equivalents", (40, 45, 50), "at"),
    )
    rows = {"income": [], "balance": [], "cash_flow": []}
    for code, tag, label, values, context_type in tags:
        if omit_capex and code == "capex":
            continue
        for year, value in zip(years, values):
            if omit_first_cash and code == "onhand" and year == 2024:
                continue
            statement = ("balance" if code == "onhand" else
                         "cash_flow" if code in {"cash", "capex"} else "income")
            rows[statement].append(f"<tr><td>{label}</td><td><ix:nonFraction name='us-gaap:{tag}' "
                                   f"contextRef='{context_type}{year}' unitRef='usd' scale='8' "
                                   f"id='{code}{year}'>{value}</ix:nonFraction></td></tr>")
    if conflict:
        rows["income"].append("<tr><td>Total revenue</td><td><ix:nonFraction "
                              "name='us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax' "
                              "contextRef='fy2026' unitRef='usd' scale='8' id='conflict'>999"
                              "</ix:nonFraction></td></tr>")
    raw = ("<html><body><ix:header><xbrli:unit id='usd'><xbrli:measure>"
           "iso4217:USD</xbrli:measure></xbrli:unit>" + contexts + "</ix:header>"
           "<h1>ITEM 8. FINANCIAL STATEMENTS AND SUPPLEMENTARY DATA</h1>"
           "<h2>INCOME STATEMENTS</h2><table>" + "".join(rows["income"]) + "</table>"
           "<h2>BALANCE SHEETS</h2><table>" + "".join(rows["balance"]) + "</table>"
           "<h2>CASH FLOWS STATEMENTS</h2><table>" +
           ("<tr><td>Net income</td><td>30</td></tr>" if duplicate_net_income_elsewhere else "") +
           "".join(rows["cash_flow"]) +
           "</table><p>Notes to financial statements.</p>"
           "<h1>ITEM 9. CHANGES IN AND DISAGREEMENTS WITH ACCOUNTANTS</h1>"
           "</body></html>").encode()
    text = extract_sec_html(raw)
    document = SimpleNamespace(pk=7, source="sec", document_type="10-k",
                               period_end=date(2027, 6, 30),
                               security=SimpleNamespace(symbol="TEST"), metadata={"cik": "1"})
    return SimpleNamespace(pk=11, document_id=7, document=document,
                           version_number=1, source_url="https://www.sec.gov/test",
                           raw_gzip=gzip.compress(raw), content_text=text)


class FinancialOverviewTests(SimpleTestCase):
    def test_three_years_and_derived_values_require_cited_base_facts(self):
        periods, rows, problem = build_financial_overview(filing())
        self.assertIsNone(problem)
        self.assertEqual([period.year for period in periods], [2025, 2026, 2027])
        by_code = {row["code"]: row for row in rows}
        self.assertEqual([cell["amount"] for cell in by_code["revenue"]["cells"]],
                         [Decimal(100), Decimal(120), Decimal(150)])
        self.assertEqual(by_code["revenue_growth"]["cells"][2]["amount"], Decimal(25))
        self.assertEqual(by_code["gross_margin"]["cells"][2]["amount"], Decimal(60))
        self.assertEqual(by_code["simple_fcf"]["cells"][2]["amount"], Decimal(20))
        self.assertEqual(by_code["revenue"]["cells"][2]["citation"]["quote"],
                         "Total revenue | 150")

    def test_conflict_and_missing_capex_do_not_become_chart_values(self):
        _, rows, _ = build_financial_overview(filing(conflict=True, omit_capex=True))
        by_code = {row["code"]: row for row in rows}
        self.assertNotIn("amount", by_code["revenue"]["cells"][2])
        self.assertIn("冲突", by_code["revenue"]["cells"][2]["status"])
        self.assertNotIn("amount", by_code["simple_fcf"]["cells"][2])
        self.assertNotIn("amount", by_code["revenue_growth"]["cells"][2])

    def test_repeated_net_income_in_cash_flow_does_not_hide_income_statement(self):
        _, rows, _ = build_financial_overview(filing(duplicate_net_income_elsewhere=True))
        net_income = next(row for row in rows if row["code"] == "net_income")
        self.assertEqual(net_income["cells"][2]["amount"], Decimal(30))

    def test_older_filing_backfills_only_same_security_and_cik(self):
        current = filing(omit_first_cash=True)
        old = filing()
        old.pk = 12
        old.document_id = 8
        old.document.pk = 8
        old.document.period_end = date(2025, 6, 30)
        current.document.security_id = old.document.security_id = 42
        periods, rows, problem = build_financial_overview(current, [old])
        self.assertIsNone(problem)
        cash = next(row for row in rows if row["code"] == "cash")
        self.assertEqual(cash["cells"][0]["amount"], Decimal(40))
        self.assertEqual(cash["cells"][0]["document_id"], 8)
        self.assertEqual(cash["cells"][0]["status"], "由该年原始 10-K 补齐")
        old.document.metadata = {"cik": "other"}
        _, rows, _ = build_financial_overview(current, [old])
        self.assertNotIn("amount", next(row for row in rows if row["code"] == "cash")["cells"][0])
