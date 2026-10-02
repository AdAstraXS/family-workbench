import copy
import gzip
from decimal import Decimal
from types import SimpleNamespace
from django.test import SimpleTestCase
from .sec_annual_release import release_rows, annual_reading, fiscal_calendar
from .sec_fact_reading import fact_tables


def fixture(currency="USD", taxonomy="us-gaap", month="December", end="12-31", start="01-01"):
    codes = ("Revenues", "NetIncomeLoss") if taxonomy == "us-gaap" else ("Revenue", "ProfitLoss")
    data = {"facts": {taxonomy: {code: {"units": {currency: [{"start": f"2025-{start}", "end": f"2025-{end}",
        "val": val * 1000000, "form": "10-K" if taxonomy == "us-gaap" else "20-F", "filed": "2026-02-01"}]}}
        for code, val in zip(codes, (100, 20))}}}
    html = f"""<html><h2>CONSOLIDATED STATEMENTS OF INCOME</h2><p>(In millions) (Unaudited)</p>
    <table><tr><td></td><td colspan="2">Year Ended</td></tr>
    <tr><td></td><td>{month} {int(end[-2:])}, 2026</td><td>{month} {int(end[-2:])}, 2025</td></tr>
    <tr><td>Revenues</td><td>120</td><td>100</td></tr>
    <tr><td>Net income</td><td>(5)</td><td>20</td></tr></table></html>"""
    v = SimpleNamespace(pk=23, media_type="text/html", raw_gzip=gzip.compress(html.encode()),
        data={"filing_date": "2027-01-15", "accession": "test"}, source_url="https://www.sec.gov/example")
    report = {"version": v, "title": "全年业绩公告", "period": f"2026-{end}"}
    return data, v, {"releases": [report]}, html


class AnnualReleaseTests(SimpleTestCase):
    def test_new_annual_values_merge_and_keep_release_provenance(self):
        data, v, overview, _ = fixture()
        rows, fiscal, notices = annual_reading(data, overview)
        self.assertEqual(len(rows), 4)
        latest = [r for r in rows if r["end"] == "2026-12-31"]
        self.assertEqual([r["value"] for r in latest], [Decimal("120000000"), Decimal("-5000000")])
        self.assertTrue(all(r["version_id"] == 23 and r["audit"].startswith("未经审计") for r in latest))
        table = fact_tables(data, rows=rows)[0]
        self.assertEqual([p["year"] for p in table["periods"]], ["2026", "2025"])
        self.assertTrue(table["periods"][0]["is_release"])
        self.assertIn("推算", fiscal["basis"])
        self.assertEqual(notices[0]["count"], 2)

    def test_foreign_issuer_ifrs_currency_is_preserved(self):
        data, v, overview, _ = fixture(currency="EUR", taxonomy="ifrs-full")
        rows = release_rows(v, data)["rows"]
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["currency"] for r in rows}, {"EUR"})
        self.assertEqual({r["standard"] for r in rows}, {"IFRS"})

    def test_quarter_titled_release_can_contain_explicit_full_year_statements(self):
        data, _, overview, _ = fixture()
        overview["releases"][0]["title"] = "业绩公告"
        rows, _, notices = annual_reading(data, overview)
        self.assertEqual(len(rows), 4)
        self.assertEqual(notices[0]["count"], 2)

    def test_formal_filing_replaces_same_year_announcement(self):
        data, v, overview, _ = fixture()
        data["facts"]["us-gaap"]["Revenues"]["units"]["USD"].append({
            "start": "2026-01-01", "end": "2026-12-31", "val": 121000000,
            "form": "10-K", "filed": "2027-02-01"})
        rows, fiscal, notices = annual_reading(data, overview)
        current = [r for r in rows if r["end"] == "2026-12-31"]
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]["value"], Decimal("121000000"))
        self.assertNotIn("source_kind", current[0])
        self.assertEqual(notices, [])

    def test_rejects_quarterly_nongaap_unknown_units_and_mismatched_comparatives(self):
        data, v, _, html = fixture()
        for changed in (html.replace("Year Ended", "Three Months Ended"),
                        html.replace("(In millions)", "(Non-GAAP, in millions)"),
                        html.replace("(In millions)", ""),
                        html.replace("Year Ended", "Year Ended Non-GAAP"),
                        html.replace(">100<", ">101<"),
                        html.replace('<td>120</td>', '<td>120%</td>'),
                        html.replace('<td>120</td>', '<td rowspan="2">120</td>')):
            v.raw_gzip = gzip.compress(changed.encode())
            self.assertEqual(release_rows(v, data)["rows"], [])

    def test_ambiguous_currency_does_not_guess_dollars(self):
        data, v, _, _ = fixture()
        for fact in data["facts"]["us-gaap"].values():
            fact["units"]["CAD"] = copy.deepcopy(fact["units"]["USD"])
        self.assertEqual(release_rows(v, data)["rows"], [])

    def test_mixed_quarter_and_annual_columns_selects_only_year(self):
        data, v, _, html = fixture()
        html = html.replace('<td colspan="2">Year Ended</td>', '<td>Quarter Ended</td><td colspan="2">Year Ended</td>')
        html = html.replace('<td>December 31, 2026</td>', '<td>December 31, 2026</td><td>December 31, 2026</td>')
        html = html.replace('<td>120</td>', '<td>40</td><td>120</td>').replace('<td>(5)</td>', '<td>3</td><td>(5)</td>')
        v.raw_gzip = gzip.compress(html.encode())
        rows = release_rows(v, data)["rows"]
        self.assertEqual([r["value"] for r in rows], [Decimal("120000000"), Decimal("-5000000")])

    def test_53_week_fiscal_calendar_uses_current_quarter_start(self):
        data, v, overview, _ = fixture()
        data["facts"]["us-gaap"]["Revenues"]["units"]["USD"] = [
            {"start": "2024-08-30", "end": "2025-08-28", "val": 1, "form": "10-K", "filed": "2025-10-03"},
            {"start": "2025-08-29", "end": "2026-05-28", "val": 2, "form": "10-Q", "filed": "2026-06-25"}]
        data["facts"]["us-gaap"].pop("NetIncomeLoss")
        overview["releases"][0]["period"] = "2026-09-03"
        fiscal = fiscal_calendar(data, overview)
        self.assertEqual((fiscal["start"], fiscal["end"], fiscal["days"]), ("2025-08-29", "2026-09-03", 371))
        self.assertIn("同期季报", fiscal["basis"])

    def test_non_calendar_year_and_no_default_dates(self):
        data, _, overview, _ = fixture(month="June", end="06-30", start="07-01")
        # Previous fiscal year began in the preceding calendar year.
        for fact in data["facts"]["us-gaap"].values():
            fact["units"]["USD"][0]["start"] = "2024-07-01"
        fiscal = fiscal_calendar(data, overview)
        self.assertEqual((fiscal["start"], fiscal["end"]), ("2025-07-01", "2026-06-30"))
        self.assertIsNone(fiscal_calendar({}, {"releases": []}))
