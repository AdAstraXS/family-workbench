from datetime import date
from decimal import Decimal

from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from .calendar import schedule, planned_events
from .housing import CITIES
from .presentation import values, profile, percentage
from .registry import SERIES
from .test_frontend import FrontendTests
from .models import MacroObservation


def spec(code, country="US"):
    return next(s for s in SERIES if s.country == country and s.code == code)


class GrowthTests(SimpleTestCase):
    def test_cpi_yoy_uses_unadjusted_and_mom_uses_adjusted(self):
        history = {("US", "CPIAUCNS", date(2024, 8, 1)): Decimal("100"),
                   ("US", "CPIAUCNS", date(2025, 8, 1)): Decimal("110"),
                   ("US", "CPIAUCSL", date(2024, 8, 1)): Decimal("200"),
                   ("US", "CPIAUCSL", date(2025, 7, 1)): Decimal("250")}
        result = values(spec("CPIAUCSL"), date(2025, 8, 1), Decimal("260"), history)
        self.assertEqual(result, {"yoy": Decimal("10.00"), "mom": Decimal("4.00")})
        del history[("US", "CPIAUCNS", date(2024, 8, 1))]
        self.assertIsNone(values(spec("CPIAUCSL"), date(2025, 8, 1), Decimal("260"), history)["yoy"])

    def test_gdp_quarterly_and_annualized_are_distinct(self):
        result = values(spec("GDPC1"), date(2025, 7, 1), Decimal("105"), {
            ("US", "GDPC1", date(2025, 4, 1)): Decimal("100"),
            ("US", "GDPC1", date(2024, 7, 1)): Decimal("100")})
        self.assertEqual(result, {"annualized": Decimal("21.55"), "yoy": Decimal("5.00"), "mom": Decimal("5.00")})

    def test_missing_period_null_and_nonpositive_base_are_not_bridged(self):
        result = values(spec("RSAFS"), date(2025, 8, 1), Decimal("105"), {
            ("US", "RSAFS", date(2025, 6, 1)): Decimal("100"),
            ("US", "RSAFS", date(2024, 8, 1)): None})
        self.assertEqual(result, {"yoy": None, "mom": None})
        for base in [Decimal("0"), Decimal("-1"), None]:
            self.assertIsNone(percentage(Decimal("100"), base))
        self.assertEqual(percentage(Decimal("0"), Decimal("100")), Decimal("-100.00"))
        self.assertEqual(percentage(Decimal("1000000000000001.1"), Decimal("1000000000000000")), Decimal("0.00"))

    def test_cn_uses_published_growth_not_cumulative_mom(self):
        s = spec("FAI_CUM", "CN")
        result = values(s, date(2025, 8, 1), Decimal("200"), {("CN", "FAI_CUM_YOY", date(2025, 8, 1)): Decimal("-2.3"),
            ("CN", "FAI_CUM", date(2024, 8, 1)): Decimal("100")})
        self.assertEqual(result, {"yoy": Decimal("-2.3")})
        self.assertEqual([m.key for m in profile(s)["modes"]], ["yoy"])
        self.assertIsNone(values(s, date(2025, 8, 1), Decimal("200"), {})["yoy"])
        self.assertEqual(values(spec("GDP_QUARTER", "CN"), date(2025, 7, 1), Decimal("30000"), {
            ("CN", "GDP_REAL_YOY_INDEX", date(2025, 7, 1)): Decimal("105.1")}), {"yoy": Decimal("5.1")})

    def test_existing_rates_and_flow_totals_are_not_percent_growth(self):
        for code in ["UNRATE", "PSAVERT", "CIVPART", "DGS2", "EFFR", "ICSA"]:
            self.assertIsNone(profile(spec(code)))
        for code in ["PMI_MANUFACTURING", "CPI_YOY", "TSF_CUM", "HOUSEHOLD_SHORT_LOANS_CUM"]:
            self.assertIsNone(profile(spec(code, "CN")))
        result = values(spec("PAYEMS"), date(2025, 8, 1), Decimal("150100"), {
            ("US", "PAYEMS", date(2025, 7, 1)): Decimal("150000")})
        self.assertEqual(result["change"], Decimal("100"))
        self.assertEqual(values(spec("HOUSE_NEW_MOM", "CN"), date(2025, 8, 1), Decimal("99.5"), {}), {"index_rate": Decimal("-0.5")})


class ScheduleTests(SimpleTestCase):
    def test_snapshot_integrity_and_timezone(self):
        data = schedule()
        self.assertEqual(len(data["events"]), 163)
        known = {(s.country, s.code) for s in SERIES}
        unique = set()
        for e in data["events"]:
            self.assertTrue(all((e["country"], c) in known for c in e["codes"]))
            key = (e["agency"], e["date"], e["time"], e["title"], e["period"])
            self.assertNotIn(key, unique)
            unique.add(key)
            self.assertEqual(e["when"].utcoffset().total_seconds(), 8 * 3600)
        summer = next(e for e in data["events"] if e["agency"] == "bls" and e["date"] == "2026-10-14")
        winter = next(e for e in data["events"] if e["agency"] == "bls" and e["date"] == "2026-12-10")
        self.assertEqual(summer["when"].hour, 20)
        self.assertEqual(winter["when"].hour, 21)
        pmi = [e for e in planned_events(date(2026, 3, 1), date(2026, 3, 31), "CN") if "采购经理" in e["title"]]
        self.assertEqual([e["day"].day for e in pmi], [4, 31])


class AddedPagesTests(TestCase):
    setUp = FrontendTests.setUp
    mapping = FrontendTests.mapping
    observation = FrontendTests.observation

    def test_sources_show_actual_range_missingness_and_separate_logs(self):
        m = self.mapping("UNRATE", "US")
        self.observation(m, date(2024, 1, 1), Decimal("4"))
        self.observation(m, date(2025, 8, 1), None)
        response = self.client.get(reverse("macro:sources"), {"country": "US", "q": "UNRATE"})
        self.assertEqual(response.context["count"], 1)
        row = response.context["rows"][0]
        self.assertEqual(row["stats"]["first"], date(2024, 1, 1))
        self.assertEqual(row["stats"]["last"], date(2025, 8, 1))
        self.assertEqual(row["stats"]["valid"], 1)
        self.assertEqual(row["missing"], 1)
        self.assertContains(response, "公开 CSV")
        self.assertContains(response, reverse("macro:status"))
        self.assertContains(self.client.get(reverse("macro:status")), "来源与历史覆盖")

    def test_housing_has_all_cities_four_exact_month_cells_and_no_fallback(self):
        m = self.mapping("HOUSE_NEW_MOM")
        self.observation(m, date(2025, 7, 1), Decimal("101.1"), "上海市")
        self.observation(m, date(2025, 8, 1), Decimal("99.8"), "北京市")
        self.observation(m, date(2025, 8, 1), None, "上海市")
        response = self.client.get(reverse("macro:housing_cities"), {"month": "2025-08"})
        self.assertEqual(len(set(CITIES)), 70)
        self.assertEqual(len(response.context["rows"]), 70)
        self.assertEqual([response.context[k] for k in ["valid", "missing", "absent"]], [1, 1, 278])
        self.assertEqual(response.context["rows"][0]["cells"][0]["change"], Decimal("-0.2"))
        self.assertContains(response, "-0.2%")
        self.assertContains(response, "指数 99.8")
        self.assertNotContains(response, "101.1")
        sh = next(r for r in response.context["rows"] if r["city"] == "上海市")
        self.assertIsNone(sh["cells"][0]["point"].value)
        detail = self.client.get(sh["cells"][0]["url"])
        self.assertEqual(detail.context["geography"], "上海市")
        absent = self.client.get(reverse("macro:housing_cities"), {"month": "2025-09"})
        self.assertEqual(absent.context["absent"], 280)
        m.indicator.is_active = False
        m.indicator.save()
        disabled = self.client.get(reverse("macro:housing_cities"), {"month": "2025-08"})
        self.assertEqual(disabled.context["disabled_count"], 70)

    def test_calendar_uses_recorded_dates_and_coalesces_cities(self):
        for code in ["HOUSE_NEW_MOM", "HOUSE_USED_YOY"]:
            m = self.mapping(code)
            for city in ["北京市", "上海市"]:
                p = self.observation(m, date(2026, 8, 1), Decimal("99"), city)
                p.release_date = date(2026, 9, 15)
                p.save()
        response = self.client.get(reverse("macro:calendar"), {"month": "2026-09", "country": "CN", "kind": "actual"})
        self.assertEqual(response.context["actual_count"], 1)
        self.assertEqual(len(response.context["days"]), 1)
        event = response.context["days"][0]["events"][0]
        self.assertEqual(len(event["indicators"]), 2)
        self.assertEqual(event["kind"], "actual")
        self.assertNotContains(response, "2026-09-15 00:00")
        unknown = self.client.get(reverse("macro:calendar"), {"month": "2027-01"})
        self.assertEqual(unknown.context["planned_count"], 0)
        self.assertContains(unknown, "尚未录入官方计划")
        for month in ["bad", "2026-13", "0000-01", "2026-01-02"]:
            self.assertEqual(self.client.get(reverse("macro:calendar"), {"month": month}).status_code, 404)

    def test_recent_releases_order_by_official_date_never_capture_or_period(self):
        m = self.mapping("UNRATE", "US")
        p = self.observation(m, date(2025, 8, 1), Decimal("4"))
        p.release_date = date(2025, 9, 5); p.save()
        n = self.mapping("CPI_YOY")
        p = self.observation(n, date(2025, 7, 1), Decimal("1"))
        p.release_date = date(2025, 9, 10); p.save()
        self.observation(m, date(2030, 1, 1), Decimal("9"))
        response = self.client.get(reverse("macro:index"))
        self.assertEqual([e["day"] for e in response.context["recent"]], [date(2025, 9, 10), date(2025, 9, 5)])
        self.assertContains(response, "最近发布")
        self.assertNotContains(response, "2030年1月")
        self.assertNotContains(response, "最近的统计期")

    def test_growth_card_chart_history_and_level_toggle_agree(self):
        m = self.mapping("CPIAUCSL", "US")
        for d,v in [(date(2024,8,1),"200"),(date(2025,7,1),"250"),(date(2025,8,1),"260")]:
            self.observation(m,d,Decimal(v))
        base = self.mapping("CPIAUCNS", "US")
        self.observation(base,date(2024,8,1),Decimal("100"))
        self.observation(base,date(2025,8,1),Decimal("110"))
        path = reverse("macro:indicator", args=["US", "CPIAUCSL"])
        response = self.client.get(path, {"range": "1"})
        self.assertEqual(response.context["growth"]["primary"]["value"], Decimal("10"))
        self.assertEqual(response.context["chart_data"]["points"][-1]["value"], "10.00")
        self.assertEqual(list(response.context["page"])[0].presentation["metrics"][1]["value"], Decimal("4"))
        self.assertContains(response, "原始值")
        raw = self.client.get(path, {"measure": "level"})
        self.assertEqual(raw.context["chart_data"]["points"][-1]["value"], "260.00000000")
        country = self.client.get(reverse("macro:country", args=["US"]))
        row = next(r for r in country.context["rows"] if r["spec"].code == "CPIAUCSL")
        self.assertEqual(row["presentation"]["primary"]["value"], Decimal("10"))
        self.assertNotIn("CPIAUCNS", [r["spec"].code for r in country.context["rows"]])

    def test_calendar_past_plan_is_not_claimed_as_actual(self):
        response = self.client.get(reverse("macro:calendar"), {"month": "2026-01", "country": "US", "kind": "planned"})
        self.assertEqual(response.context["actual_count"], 0)
        self.assertContains(response, "实际发布日期未核验")
        self.assertTrue(all(e["kind"] == "planned" for d in response.context["days"] for e in d["events"]))
