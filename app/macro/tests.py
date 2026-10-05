from datetime import date
from decimal import Decimal
from io import StringIO
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from threading import Event

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import close_old_connections, connection
from django.test import SimpleTestCase, TestCase, TransactionTestCase
from django.urls import reverse

from family_core.models import Family, FamilyMember
from .adapters import SourceError, number, parse_frame, parse_fred, parse_official, period_date
from .fetch_worker import validate_url
from .models import MacroDataPoint, MacroImportRun, MacroIndicator, MacroObservation, MacroObservationRevision, MacroSourceMapping
from .registry import GROUPS, SERIES
from .services import import_group, prepare


def fred_payload(value="2.1", extra=""):
    return {"url": "https://fred.stlouisfed.org/graph/fredgraph.csv?id=UNRATE",
            "text": f"observation_date,UNRATE\n2025-09-01,{value}\n2025-10-01,.\n" + extra}


class ParserTests(SimpleTestCase):
    def test_periods_and_decimal(self):
        for label in ["2025年10月份", "2025年10月", "202510", "2025-10"]:
            self.assertEqual(period_date(label), date(2025, 10, 1))
        self.assertEqual(period_date("2025年第三季度"), date(2025, 7, 1))
        self.assertEqual(number("1,234.125"), Decimal("1234.125"))
        self.assertIsNone(number("."))
        for bad in ["Infinity", "1e20", "1.000000001", "暂无"]:
            with self.assertRaises(SourceError):
                number(bad)

    def test_fred_null_is_not_zero_or_previous_value(self):
        points = parse_fred(fred_payload()["text"], GROUPS["fred_UNRATE"][0])
        self.assertIsNone(points[1].value)
        self.assertIsNone(points[0].release_date)
        with self.assertRaises(SourceError):
            parse_fred("observation_date,CPI\n2025-09-01,10", GROUPS["fred_UNRATE"][0])

    def test_nbs_index_and_unemployment_selector(self):
        points = parse_frame([{"index": "国内生产总值指数(上年同期=100)当季值", "2025年第一季度": "105.4"}], GROUPS["nbs_gdp_index"])
        self.assertEqual(points[0].value, Decimal("105.4"))
        points = parse_frame([{"date": "202508", "item": "全国城镇调查失业率 ", "value": "5.2"},
                              {"date": "202508", "item": "其他对象", "value": "9"}], GROUPS["cn_unemployment"])
        self.assertEqual(len(points), 1)
        with self.assertRaises(SourceError):
            parse_frame([{"index": "错误指标", "2025年1月": "1"}], GROUPS["nbs_gdp_index"])

    def test_fai_does_not_use_monthly_growth_column(self):
        points = parse_frame([{"月份": "2025年08月份", "自年初累计": "293092", "同比增长": "-13.51"}], GROUPS["cn_investment"])
        self.assertEqual(points[0].value, Decimal("293092"))

    def test_trade_scales_thousand_dollars_to_100million(self):
        points = parse_frame([{"月份": "2025年08月份", "当月出口额-金额": "401440956.924", "当月进口额-金额": "282355763.276"}], GROUPS["cn_trade"])
        self.assertEqual(points[0].value, Decimal("4014.40956924"))

    def test_duplicates_and_empty_source_fail(self):
        for payload in [fred_payload(extra="2025-09-01,3\n"), {"text": "observation_date,UNRATE\n"}]:
            with self.assertRaises(SourceError):
                prepare("fred_UNRATE", payload)

    def test_mofcom_signed_cumulative_and_metadata(self):
        html = '''<meta name="PubDate" content="2025-09-18"><h1>2025年1-8月全国吸收外资4799.5亿元人民币</h1>
        <p>全国新设立外商投资企业42582家；实际使用外资金额4799.5亿元人民币，同比下降5.3%。</p>'''
        values = {p.code: p for p in parse_official(html, "mofcom")}
        self.assertEqual(values["FDI_CUM_YOY"].value, Decimal("-5.3"))
        self.assertEqual(values["FDI_CUM"].period, date(2025, 8, 1))
        self.assertEqual(values["FDI_CUM"].release_date, date(2025, 9, 18))
        with self.assertRaises(SourceError):
            parse_official(html.replace("亿元人民币", "亿美元"), "mofcom")

    def test_pbc_negative_loans_and_unit_conversion(self):
        html = '''2025年8月金融统计数据报告。社会融资规模存量为464.8万亿元，同比增长7.2%。
        社会融资规模增量累计为23.91万亿元。政府债券余额103.69万亿元。政府债券净融资8.77万亿元。
        住户贷款减少1.03万亿元，其中，短期贷款减少1.05万亿元，中长期贷款增加188亿元；
        企（事）业单位贷款增加11.26万亿元，其中，短期贷款增加4.18万亿元，中长期贷款增加5.64万亿元，票据融资增加1.29万亿元。'''
        values = {p.code: p.value for p in parse_official(html, "pbc")}
        self.assertEqual(values["HOUSEHOLD_LOANS_CUM"], Decimal("-10300"))
        self.assertEqual(values["HOUSEHOLD_LONG_LOANS_CUM"], Decimal("188"))
        self.assertEqual(values["COMPANY_LONG_LOANS_CUM"], Decimal("56400"))
        self.assertEqual(values["TSF_CUM"], Decimal("239100"))

    def test_infrastructure_requires_scope_note(self):
        text = "2025年1—8月份固定资产投资。基础设施投资（口径详见附注1）同比下降4.0%。"
        with self.assertRaises(SourceError):
            parse_official(text, "nbs_release")
        text += "基础设施投资：按现行制度包括电力生产供应、水利、交通等领域。基础设施投资增速按可比口径计算。"
        self.assertEqual(parse_official(text, "nbs_release")[0].value, Decimal("-4.0"))

    def test_url_allowlist(self):
        validate_url("https://www.pbc.gov.cn/report.html", "pbc")
        for url in ["http://www.pbc.gov.cn/", "https://127.0.0.1/", "https://www.pbc.gov.cn.evil.org/", "https://user:pass@www.pbc.gov.cn/", "https://www.pbc.gov.cn:444/"]:
            with self.assertRaises(ValueError):
                validate_url(url, "pbc")

    def test_dictionary_uniqueness(self):
        self.assertEqual(len(SERIES), len({(s.country, s.code) for s in SERIES}))


class ImportTests(TestCase):
    def ingest(self, payload=None, write=True):
        return import_group("fred_UNRATE", write=write, fetcher=lambda *_: payload or fred_payload())

    def test_dry_run_has_no_database_writes(self):
        with self.assertNumQueries(0):
            result = self.ingest(write=False)
        self.assertEqual(result["points"], 2)
        self.assertFalse(MacroImportRun.objects.exists())

    def test_idempotent_and_revisions_can_restore_old_value(self):
        self.assertEqual(self.ingest()["created"], 2)
        self.assertEqual(self.ingest()["unchanged"], 2)
        self.assertEqual(MacroObservationRevision.objects.count(), 2)
        self.assertEqual(self.ingest(fred_payload("2.2"))["revised"], 1)
        self.assertEqual(self.ingest(fred_payload("2.1"))["revised"], 1)
        point = MacroObservation.objects.get(period_date="2025-09-01")
        self.assertEqual(point.revision, 3)
        self.assertEqual(list(point.revisions.values_list("value", flat=True)), [Decimal("2.1"), Decimal("2.2"), Decimal("2.1")])
        self.assertIsNone(MacroObservation.objects.get(period_date="2025-10-01").value)

    def test_failure_audited_without_partial_change(self):
        self.ingest()
        with self.assertRaises(SourceError):
            self.ingest(fred_payload("6", "2025-09-01,7\n"))
        self.assertEqual(MacroObservation.objects.get(period_date="2025-09-01").value, Decimal("2.1"))
        self.assertEqual(MacroImportRun.objects.order_by("pk").last().status, "failed")

    def test_mid_write_failure_rolls_back_everything(self):
        with patch("macro.services.MacroObservationRevision.objects.create", side_effect=RuntimeError("private detail")):
            with self.assertRaises(SourceError):
                self.ingest()
        self.assertFalse(MacroObservation.objects.exists())
        self.assertFalse(MacroSourceMapping.objects.exists())
        run = MacroImportRun.objects.get()
        self.assertEqual(run.status, "failed")
        self.assertNotIn("private detail", run.error)

    def test_legacy_data_untouched(self):
        indicator = MacroIndicator.objects.create(country="CN", code="legacy", name="旧记录")
        MacroDataPoint.objects.create(indicator=indicator, period_date="2020-01-01", value=1)
        MacroDataPoint.objects.create(indicator=indicator, period_date="2020-01-01", value=2)
        self.ingest()
        self.assertEqual(MacroDataPoint.objects.count(), 2)

    def test_definition_change_rejected(self):
        self.ingest()
        mapping = MacroSourceMapping.objects.get()
        mapping.definition_hash = "changed"
        mapping.save()
        with self.assertRaises(SourceError):
            self.ingest()

    def test_disabled_indicator_rejected(self):
        self.ingest()
        MacroIndicator.objects.update(is_active=False)
        with self.assertRaises(SourceError):
            self.ingest()

    def test_command_failure_is_nonzero_and_dry_run_default(self):
        with patch("macro.services.fetch_source", return_value=fred_payload()):
            call_command("import_macro", group=["fred_UNRATE"], stdout=StringIO())
        self.assertFalse(MacroImportRun.objects.exists())
        with patch("macro.services.fetch_source", side_effect=SourceError("offline")):
            with self.assertRaises(CommandError):
                call_command("import_macro", group=["fred_UNRATE"], write=True, stdout=StringIO(), stderr=StringIO())
        self.assertEqual(MacroImportRun.objects.get().status, "failed")

    def test_member_read_only_views_and_country_separation(self):
        self.ingest()
        family = Family.objects.create(name="测试家庭")
        user = get_user_model().objects.create_user(username="macro-viewer")
        member = FamilyMember.objects.create(family=family, user=user, display_name="查看者", role=FamilyMember.ROLE_VIEWER)
        self.client.force_login(user)
        mapping = MacroSourceMapping.objects.get()
        point = MacroObservation.objects.first()
        paths = [reverse("macro:index"), reverse("macro:detail", args=[mapping.pk]),
                 reverse("macro:revisions", args=[point.pk]), reverse("macro:status")]
        before = MacroImportRun.objects.count()
        with patch("macro.services.fetch_source", side_effect=AssertionError("GET must not fetch")):
            for path in paths:
                self.assertEqual(self.client.get(path).status_code, 200)
                self.assertEqual(self.client.post(path).status_code, 403)
        self.assertEqual(MacroImportRun.objects.count(), before)
        self.assertNotContains(self.client.get(paths[0]), "核心个人消费支出价格指数")
        self.assertContains(self.client.get(paths[0] + "?country=US"), "美国宏观数据")
        self.assertNotContains(self.client.get(paths[0] + "?country=US"), "民间固定资产投资")
        member.is_active = False
        member.save()
        self.assertEqual(self.client.get(paths[0]).status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.get(paths[0]).status_code, 302)


class ConcurrentImportTests(TransactionTestCase):
    def test_older_slow_fetch_cannot_overwrite_newer_completed_run(self):
        if connection.vendor != "postgresql":
            self.skipTest("row-lock verification requires PostgreSQL")
        started, release = Event(), Event()

        def slow_fetch(*_):
            started.set()
            if not release.wait(15):
                raise RuntimeError("test coordination timeout")
            return fred_payload("1")

        def slow_job():
            close_old_connections()
            try:
                import_group("fred_UNRATE", write=True, fetcher=slow_fetch)
            except SourceError:
                return "superseded"
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=1) as pool:
            slow = pool.submit(slow_job)
            try:
                self.assertTrue(started.wait(10))
                import_group("fred_UNRATE", write=True, fetcher=lambda *_: fred_payload("3"))
            finally:
                release.set()
            self.assertEqual(slow.result(timeout=15), "superseded")
        self.assertEqual(MacroObservation.objects.get(period_date="2025-09-01").value, Decimal("3"))
        self.assertEqual(MacroObservation.objects.count(), 2)
        self.assertEqual(MacroObservationRevision.objects.count(), 2)
