from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from family_core.models import Family, FamilyMember
from .guides import GUIDES, BASICS
from .models import MacroIndicator, MacroSourceMapping, MacroObservation, MacroObservationRevision, MacroImportRun
from .registry import SERIES
from .services import import_group
from .templatetags.macro_format import macro_number, macro_period
from .tests import fred_payload


class GuideContentTests(SimpleTestCase):
    def test_every_registered_indicator_has_complete_source_backed_guide(self):
        self.assertEqual(set(GUIDES), {(s.country, s.code) for s in SERIES})
        self.assertEqual(len(GUIDES), len(SERIES))
        for key, guide in GUIDES.items():
            with self.subTest(indicator=key):
                for field in ["title", "lead", "scope", "calculation", "formula", "example", "method", "meaning", "kind", "aliases"]:
                    self.assertTrue(guide[field].strip(), field)
                self.assertGreaterEqual(len(guide["pitfalls"]), 2)
                self.assertTrue(guide["sources"])
                for title, url in guide["sources"]:
                    self.assertTrue(title)
                    self.assertTrue(url.startswith("https://"))
                for related in guide["related"]:
                    target = tuple(related.split(":"))
                    self.assertIn(target, GUIDES)
                    self.assertEqual(target[0], key[0])
        self.assertGreaterEqual(len(BASICS), 6)

    def test_sensitive_statistical_distinctions_are_explicit(self):
        self.assertIn("净", GUIDES[("US", "PAYEMS")]["pitfalls"][1])
        self.assertIn("利息", GUIDES[("US", "PSAVERT")]["scope"])
        self.assertIn("中位", GUIDES[("US", "EFFR")]["formula"])
        self.assertIn("不可", GUIDES[("CN", "FDI_HIGHTECH_CUM")]["pitfalls"][2])
        self.assertIn("单季", GUIDES[("CN", "GDP_QUARTER")]["pitfalls"][2])
        self.assertIn("指数 − 100", GUIDES[("CN", "GDP_REAL_YOY_INDEX")]["formula"])

    def test_exact_number_and_period_rendering(self):
        self.assertEqual(macro_number(Decimal("1234567890123456.12345678")), "1234567890123456.12345678")
        self.assertEqual(macro_number(Decimal("0")), "0")
        self.assertEqual(macro_number(None), "来源缺值")
        self.assertEqual(macro_period(date(2025, 4, 1), "季度"), "2025年 第2季度")
        self.assertEqual(macro_period(date(2025, 1, 1), "年度"), "2025年")


class FrontendTests(TestCase):
    def setUp(self):
        family = Family.objects.create(name="宏观测试家庭")
        self.user = get_user_model().objects.create_user(username="macro-reader")
        self.member = FamilyMember.objects.create(family=family, user=self.user, display_name="成员", role=FamilyMember.ROLE_MEMBER)
        self.client.force_login(self.user)

    def mapping(self, code, country="CN"):
        spec = next(s for s in SERIES if s.country == country and s.code == code)
        indicator = MacroIndicator.objects.create(country=country, code=code, name=spec.name, unit=spec.unit)
        return MacroSourceMapping.objects.create(indicator=indicator, provider=spec.provider, group=spec.group,
                                                definition=spec.definition(), definition_hash="test")

    def observation(self, mapping, period, value, geography="全国"):
        return MacroObservation.objects.create(mapping=mapping, period_date=period, value=value, geography=geography,
                                               last_seen_at=timezone.now(), fingerprint="test")

    def test_all_guides_and_unconnected_indicators_render(self):
        for spec in SERIES:
            with self.subTest(code=spec.code):
                response = self.client.get(reverse("macro:guide", args=[spec.country, spec.code]))
                self.assertContains(response, "假设数字")
                self.assertContains(response, "方法来源")
                self.assertContains(response, reverse("macro:indicator", args=[spec.country, spec.code]))
                self.assertContains(self.client.get(reverse("macro:indicator", args=[spec.country, spec.code])), "尚未接入")

    def test_search_country_theme_and_pagination(self):
        path = reverse("macro:encyclopedia")
        response = self.client.get(path, {"q": "非农", "country": "US", "theme": "就业"})
        self.assertTrue(any(r["spec"].code == "PAYEMS" for r in response.context["rows"]))
        self.assertTrue(all(r["spec"].country == "US" and r["spec"].category == "就业" for r in response.context["rows"]))
        response = self.client.get(path, {"country": "CN", "theme": "全部"})
        self.assertEqual(len(response.context["rows"]), 12)
        self.assertIn("country=CN", response.context["next_url"])
        self.assertEqual(self.client.get(response.context["next_url"]).context["page"].number, 2)
        self.assertContains(self.client.get(path, {"q": "no-such-indicator"}), "没有匹配")
        self.assertNotContains(self.client.get(path, {"q": '<script>alert("x")</script>'}), '<script>alert("x")</script>')

    def test_chart_geography_latest_null_and_range_are_consistent(self):
        mapping = self.mapping("HOUSE_NEW_MOM")
        self.observation(mapping, date(2020, 1, 1), Decimal("98"), "北京市")
        self.observation(mapping, date(2024, 8, 1), Decimal("99.8"), "北京市")
        self.observation(mapping, date(2025, 8, 1), None, "北京市")
        self.observation(mapping, date(2025, 8, 1), Decimal("100.12345678"), "上海市")
        path = reverse("macro:indicator", args=["CN", "HOUSE_NEW_MOM"])
        response = self.client.get(path, {"range": "1"})
        self.assertEqual(response.context["geography"], "北京市")
        self.assertEqual(response.context["chart_count"], 2)
        self.assertIsNone(response.context["latest"].value)
        self.assertIsNone(response.context["chart_data"]["points"][-1]["value"])
        self.assertEqual(response.context["chart_data"]["reference"], "0")
        response = self.client.get(path, {"range": "all", "geography": "上海市", "measure": "level"})
        self.assertEqual(response.context["chart_count"], 1)
        self.assertEqual(response.context["chart_data"]["points"][0]["value"], "100.12345678")
        self.assertContains(response, "尚不能形成趋势")
        self.assertEqual(self.client.get(path, {"geography": "不存在的城市"}).status_code, 404)
        self.assertEqual(self.client.get(path, {"range": "all"}).context["chart_count"], 3)

    def test_history_pagination_retains_region_range_and_country_selection(self):
        mapping = self.mapping("HOUSE_USED_YOY")
        for year in [2023, 2024, 2025]:
            for month in range(1, 13):
                self.observation(mapping, date(year, month, 1), Decimal("100"), "北京市")
        path = reverse("macro:indicator", args=["CN", "HOUSE_USED_YOY"])
        response = self.client.get(path, {"range": "all", "geography": "北京市"})
        self.assertEqual(response.context["chart_count"], 36)
        next_page = self.client.get(response.context["next_url"])
        self.assertEqual(next_page.context["chart_count"], 36)
        self.assertEqual(len(next_page.context["page"]), 6)
        self.assertEqual(next_page.context["geography"], "北京市")
        self.assertEqual(next_page.context["range"], "all")

    def test_city_is_preserved_between_country_detail_and_guide(self):
        mapping = self.mapping("HOUSE_NEW_MOM")
        self.observation(mapping, date(2025, 8, 1), Decimal("100.4"), "上海市")
        self.observation(mapping, date(2025, 8, 1), Decimal("99.8"), "北京市")
        response = self.client.get(reverse("macro:country", args=["CN"]), {
            "theme": "房地产", "series": "HOUSE_NEW_MOM", "geography": "上海市"})
        detail = self.client.get(response.context["selected"]["url"])
        self.assertEqual(detail.context["geography"], "上海市")
        guide = self.client.get(detail.context["guide_url"])
        actual = self.client.get(guide.context["row"]["url"])
        self.assertEqual(actual.context["geography"], "上海市")
        self.assertEqual(actual.context["latest"].value, Decimal("100.4"))

    def test_read_only_permissions_and_no_fetch_or_database_writes(self):
        import_group("fred_UNRATE", write=True, fetcher=lambda *_: fred_payload())
        point = MacroObservation.objects.first()
        paths = [reverse("macro:index"), reverse("macro:country", args=["CN"]), reverse("macro:country", args=["US"]),
                 reverse("macro:indicator", args=["US", "UNRATE"]), reverse("macro:guide", args=["US", "UNRATE"]),
                 reverse("macro:encyclopedia"), reverse("macro:sources"), reverse("macro:status"), reverse("macro:calendar"),
                 reverse("macro:housing_cities"), reverse("macro:revisions", args=[point.pk])]
        models = [MacroIndicator, MacroSourceMapping, MacroObservation, MacroObservationRevision, MacroImportRun]
        before = [m.objects.count() for m in models]
        with patch("macro.services.fetch_source", side_effect=AssertionError("GET cannot fetch")):
            for path in paths:
                self.assertEqual(self.client.get(path).status_code, 200)
                self.assertEqual(self.client.post(path).status_code, 405)
        self.assertEqual([m.objects.count() for m in models], before)
        self.member.role = FamilyMember.ROLE_VIEWER
        self.member.save()
        for path in paths:
            self.assertEqual(self.client.get(path).status_code, 200)
            self.assertEqual(self.client.post(path).status_code, 403)
        self.member.is_active = False
        self.member.save()
        for path in paths:
            self.assertEqual(self.client.get(path).status_code, 403)
        self.client.logout()
        for path in paths:
            self.assertEqual(self.client.get(path).status_code, 302)

    def test_invalid_and_disabled_series_are_not_exposed(self):
        self.assertEqual(self.client.get(reverse("macro:indicator", args=["CN", "unknown"])).status_code, 404)
        self.assertEqual(self.client.get(reverse("macro:country", args=["ZZ"])).status_code, 404)
        mapping = self.mapping("CPI_MOM")
        mapping.indicator.is_active = False
        mapping.indicator.save()
        self.assertEqual(self.client.get(reverse("macro:indicator", args=["CN", "CPI_MOM"])).status_code, 404)
        self.assertEqual(self.client.get(reverse("macro:guide", args=["CN", "CPI_MOM"])).status_code, 404)
        codes = {r["spec"].code for r in self.client.get(reverse("macro:encyclopedia"), {"q": "CPI_MOM"}).context["rows"]}
        self.assertNotIn("CPI_MOM", codes)

    def test_country_focus_and_separation(self):
        response = self.client.get(reverse("macro:country", args=["CN"]), {"theme": "投资"})
        self.assertEqual(response.context["spec"].code, "FAI_CUM_YOY")
        self.assertNotContains(response, "核心个人消费支出价格指数")
        response = self.client.get(reverse("macro:country", args=["US"]), {"theme": "通胀"})
        self.assertContains(response, "核心个人消费支出价格指数")
        self.assertNotContains(response, "民间固定资产投资")
