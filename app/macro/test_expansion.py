from datetime import date
from decimal import Decimal

from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from .adapters import SourceError, parse_frame, parse_official
from .presentation import CN_PAIRS, presentation, values
from .registry import SERIES, GROUPS
from .test_frontend import FrontendTests
from .views import PANORAMA


def spec(code, country="US"):
    return next(s for s in SERIES if s.code == code and s.country == country)


class ExpansionContractTests(SimpleTestCase):
    def test_age_series_never_connect_old_student_inclusive_data(self):
        rows = [{"index": s.field, "2023年6月": "21.3", "2023年12月": "14.9",
                 "2025年8月": "."} for s in GROUPS["nbs_unemployment_age"]]
        points = parse_frame(rows, GROUPS["nbs_unemployment_age"])
        self.assertEqual(len(points), 6)
        self.assertEqual({p.period for p in points}, {date(2023, 12, 1), date(2025, 8, 1)})
        self.assertTrue(all(p.value == Decimal("14.9") for p in points if p.period.year == 2023))
        with self.assertRaises(SourceError):
            parse_frame(rows[:2], GROUPS["nbs_unemployment_age"])

    def test_ism_uses_report_month_and_checks_sector(self):
        html = '<meta name="PubDate" content="2025-10-01"><h1>Manufacturing PMI® at 49.1%</h1><h2>September 2025 ISM® Manufacturing PMI® Report</h2>'
        point = parse_official(html, "ism_manufacturing")[0]
        self.assertEqual((point.period, point.release_date, point.value),
                         (date(2025, 9, 1), date(2025, 10, 1), Decimal("49.1")))
        with self.assertRaises(SourceError):
            parse_official(html, "ism_services")
        with self.assertRaises(SourceError):
            parse_official("<h1>Access denied</h1>", "ism_manufacturing")

    def test_budget_ratio_is_annual_plan_with_verified_year(self):
        html = '<meta name="firstpublishedtime" content="2026-03-13-21:00:00"><h1>政府工作报告</h1><p>2026年政府工作任务。今年赤字率拟按4%左右安排。</p>'
        point = parse_official(html, "gov_budget")[0]
        self.assertEqual(point.period, date(2026, 1, 1))
        self.assertEqual(point.value, Decimal("4"))
        self.assertIn("预算", point.evidence["source_notes"])
        for bad in [html.replace("2026年", "2025年"), html.split("<h1>")[1]]:
            with self.assertRaises(SourceError):
                parse_official(bad, "gov_budget")

    def test_ppi_yoy_uses_unadjusted_not_adjusted(self):
        history = {("US", "PPIFID", date(2024, 8, 1)): Decimal("100"),
                   ("US", "PPIFID", date(2025, 8, 1)): Decimal("110"),
                   ("US", "PPIFIS", date(2025, 7, 1)): Decimal("200")}
        result = values(spec("PPIFIS"), date(2025, 8, 1), Decimal("202"), history)
        self.assertEqual(result, {"yoy": Decimal("10"), "mom": Decimal("1")})
        del history[("US", "PPIFID", date(2025, 8, 1))]
        self.assertIsNone(values(spec("PPIFIS"), date(2025, 8, 1), Decimal("202"), history)["yoy"])

    def test_paired_amounts_show_levels_and_keep_separate_rates(self):
        for code, rate_code in CN_PAIRS.items():
            with self.subTest(code=code):
                p = presentation(spec(code, "CN"), date(2025, 8, 1), Decimal("1234"),
                                 {("CN", rate_code, date(2025, 8, 1)): Decimal("105")})
                self.assertEqual(p["default"], "level")
                self.assertEqual(p["primary"]["value"], Decimal("1234"))
        fallback = presentation(spec("GOVERNMENT_BONDS_STOCK", "CN"), date(2025, 8, 1), Decimal("0"), {})
        self.assertEqual(fallback["primary"]["value"], Decimal("0"))
        self.assertEqual(fallback["default"], "level")

    def test_annual_nominal_gdp_yoy_uses_exact_previous_year(self):
        p = presentation(spec("GDP_ANNUAL", "CN"), date(2025, 1, 1), Decimal("1401879.2"),
                         {("CN", "GDP_ANNUAL", date(2024, 1, 1)): Decimal("1348066.2")})
        self.assertEqual(p["primary"]["label"], "名义同比")
        self.assertEqual(p["primary"]["value"], Decimal("3.99"))


class ExpansionPagesTests(TestCase):
    setUp = FrontendTests.setUp
    mapping = FrontendTests.mapping
    observation = FrontendTests.observation

    def test_all_page_has_one_representative_for_each_theme(self):
        for country in ["CN", "US"]:
            response = self.client.get(reverse("macro:country", args=[country]), {"theme": "全部"})
            self.assertContains(response, "经济全景")
            self.assertNotContains(response, "macro-summary-grid")
            self.assertNotContains(response, 'name="series"')
            self.assertNotContains(response, 'class="macro-original"')
            rows = response.context["panorama"]
            self.assertEqual(len(rows), len(PANORAMA[country]))
            self.assertEqual(len({r["spec"].category for r in rows}), len(rows))
            self.assertIsNone(response.context["selected"])

    def test_amount_and_rate_are_distinct_and_chart_defaults_to_amount(self):
        for code, rate, category in [("RETAIL", "RETAIL_YOY", "消费"), ("INDUSTRIAL_PROFIT_CUM", "INDUSTRIAL_PROFIT_CUM_YOY", "生产")]:
            self.observation(self.mapping(code), date(2025, 8, 1), Decimal("12345"))
            self.observation(self.mapping(rate), date(2025, 8, 1), Decimal("5.2"))
            response = self.client.get(reverse("macro:country", args=["CN"]), {"theme": category, "series": code})
            self.assertEqual(response.context["growth"]["primary"]["value"], Decimal("12345"))
            self.assertEqual(response.context["chart_data"]["points"][-1]["value"], "12345.00000000")
            self.assertContains(response, "12345 亿元")
            self.assertContains(response, "5.2 %")

    def test_left_navigation_removes_housing_but_theme_keeps_entry(self):
        response = self.client.get(reverse("macro:country", args=["CN"]), {"theme": "房地产"})
        html = response.content.decode()
        sidebar = html.split('<aside class="macro-sidebar">')[1].split("</aside>")[0]
        self.assertNotIn(reverse("macro:housing_cities"), sidebar)
        self.assertContains(response, reverse("macro:housing_cities"))
