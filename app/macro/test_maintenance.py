from datetime import date, timedelta
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import SimpleTestCase, TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone
from family_core.models import Family, FamilyMember

from .adapters import SourceError, parse_official
from .calendar import current_schedule
from .calendar_maintenance import parse_ics, refresh_calendars
from .discovery import ReportLink, discover, links, title_period
from .health import update_health
from .maintenance import maintain, maintenance_lock, recent_start, update_official
from .models import MacroCalendarSnapshot, MacroMaintenanceRun, MacroObservation, MacroOfficialReport

URL = "https://www.mofcom.gov.cn/xwfb/rcxwfb/art/2025/report.html"
FDI = '<meta name="PubDate" content="2025-09-18"><h1>2025年1-8月全国吸收外资</h1><p>全国新设立外商投资企业42582家，实际使用外资金额4799.5亿元人民币，同比下降5.3%。</p>'
REPORT = ReportLink("mofcom", "2025年1-8月全国吸收外资", URL, date(2025, 8, 1))
ICS = '''BEGIN:VCALENDAR
BEGIN:VEVENT
SUMMARY:Personal Income and Outlays\\, August 2026
DTSTART;VALUE=DATE-TIME:20260925T123000Z
END:VEVENT
END:VCALENDAR'''


class DiscoveryTests(SimpleTestCase):
    def test_period_labels(self):
        for title, month in [("2025年一季度金融统计数据报告", 3), ("2025年上半年金融统计数据报告", 6),
                             ("2025年前三季度金融统计数据报告", 9), ("2025年金融统计数据报告", 12),
                             ("2025年1—8月全国吸收外资", 8)]:
            self.assertEqual(title_period(title), date(2025, month, 1))

    def test_closed_host_and_duplicate_anchors(self):
        html = f'<a href="{URL}">{REPORT.title}</a>' * 2
        self.assertEqual(len(links(html, "mofcom", URL)), 1)
        with self.assertRaises(SourceError):
            links(html.replace("www.mofcom.gov.cn", "evil.org"), "mofcom", URL)

    def test_repeated_pagination_fails(self):
        html = '<a href="/diaochatongjisi/116219/116225/report.html">2026年8月金融统计数据报告</a>'
        with self.assertRaisesMessage(SourceError, "分页重复"):
            discover("pbc", date(2025, 1, 1), date(2026, 10, 1), pages=2, reader=lambda _: html)

    def test_empty_catalogue_is_not_success(self):
        with self.assertRaises(SourceError):
            discover("pbc", date(2025, 1, 1), date(2026, 10, 1), pages=1, reader=lambda _: '<html>登录</html>')

    def test_calendar_page_url_rejects_private_hosts_and_credentials(self):
        from .http_worker import validate_page_url
        validate_page_url("https://www.bea.gov/calendar.ics")
        for bad in ["http://www.bls.gov/", "https://127.0.0.1/", "https://www.bea.gov:444/", "https://user:pass@www.gov.cn/"]:
            with self.assertRaises(ValueError):
                validate_page_url(bad)

    def test_first_month_and_annual_fdi(self):
        self.assertEqual(parse_official(FDI.replace("1-8月", "1月"), "mofcom")[0].period, date(2025, 1, 1))
        self.assertEqual(parse_official(FDI.replace("1-8月", "全年"), "mofcom")[0].period, date(2025, 12, 1))

    def test_publication_metadata_slashes_and_reordered_attributes(self):
        html = FDI.replace('<meta name="PubDate" content="2025-09-18">', '<meta content="2025/09/18 10:00" name="PubDate">')
        self.assertEqual(parse_official(html, 'mofcom')[0].release_date, date(2025, 9, 18))

    def test_split_pbc_reports_and_cumulative_guard(self):
        stock = '2025年8月社会融资规模存量统计数据报告。社会融资规模存量为433.66万亿元，同比增长8.8%。政府债券余额为91.36万亿元。'
        self.assertEqual(len(parse_official(stock, "pbc")), 3)
        flow = '2025年8月社会融资规模增量统计数据报告。前八个月社会融资规模增量累计为26.56万亿元，政府债券净融资10.27万亿元。8月份社会融资规模增量为2万亿元，政府债券净融资1万亿元。'
        self.assertEqual(parse_official(flow, "pbc")[1].value, 102700)
        with self.assertRaises(SourceError):
            parse_official(stock.replace("政府债券余额为91.36万亿元", ""), "pbc")
        january = '2026年1月社会融资规模增量统计数据报告。1月份社会融资规模增量为7.22万亿元，政府债券净融资9764亿元。'
        self.assertEqual(parse_official(january, 'pbc')[0].value, Decimal('72200'))
        with self.assertRaises(SourceError):
            parse_official(january.replace('1月', '2月'), 'pbc')

    def test_ics_actual_time_and_folded_title(self):
        parsed = parse_ics(ICS.replace("Personal Income and Outlays", "Personal Income and \n Outlays"), "bea")
        self.assertEqual(parsed[0]["time"], "08:30")
        self.assertEqual(parsed[0]["period"], "Personal Income and Outlays, August 2026")

    def test_recent_window_allows_unpublished_current_month(self):
        self.assertEqual(recent_start(date(2026, 10, 5)), date(2026, 8, 1))
        self.assertEqual(recent_start(date(2026, 1, 1)), date(2025, 11, 1))

    def test_infrastructure_scopes_do_not_share_one_series(self):
        points = parse_official('2025年1—2月份固定资产投资。基础设施投资（不含电力、热力、燃气及水生产和供应业）同比增长5.6%。', 'nbs_release')
        self.assertEqual(points[0].code, 'INFRASTRUCTURE_EX_UTILITIES_CUM_YOY')
        annual = parse_official('2025年全国固定资产投资基本情况。基础设施投资（不含电力、热力、燃气及水生产和供应业）比上年下降2.2%。', 'nbs_release')
        self.assertEqual(annual[0].period, date(2025, 12, 1))
        self.assertEqual(annual[0].value, Decimal('-2.2'))


class OfficialUpdateTests(TestCase):
    def update(self, text=FDI, write=True):
        with patch("macro.maintenance.discover", return_value=[REPORT]):
            return update_official(date(2025, 1, 1), date(2025, 12, 1), write=write, groups=["mofcom"],
                                   fetcher=lambda *_: {"text": text, "url": URL})

    def test_preview_does_not_write(self):
        self.assertFalse(self.update(write=False)["failures"])
        self.assertEqual(MacroOfficialReport.objects.count(), 0)
        self.assertEqual(MacroObservation.objects.count(), 0)

    def test_repeat_is_idempotent_and_revisions_are_kept(self):
        self.assertFalse(self.update()["failures"])
        self.assertFalse(self.update()["failures"])
        self.assertEqual(MacroObservation.objects.count(), 3)
        self.assertEqual(sum(p.revision for p in MacroObservation.objects.all()), 3)
        self.update(FDI.replace("4799.5", "4799.6"))
        self.assertEqual(MacroObservation.objects.get(mapping__indicator__code="FDI_CUM").revision, 2)

    def test_bad_period_and_missing_release_do_not_overwrite(self):
        self.update()
        for bad in [FDI.replace("1-8月", "1-7月"), FDI.replace('<meta name="PubDate" content="2025-09-18">', '')]:
            self.assertTrue(self.update(bad)["failures"])
            self.assertEqual(MacroObservation.objects.count(), 3)
            self.assertEqual(MacroOfficialReport.objects.get().status, "failed")

    def test_calendar_failure_retains_old_snapshot(self):
        MacroCalendarSnapshot.objects.create(agency="bea", source_url="https://www.bea.gov/calendar.ics", content_hash="a"*64,
            payload={"events": parse_ics(ICS, "bea")}, checked_at=timezone.now())
        result = refresh_calendars(write=True, reader=lambda _: 'not a calendar')
        self.assertEqual(len(result["failures"]), 4)
        self.assertEqual(MacroCalendarSnapshot.objects.count(), 1)
        self.assertTrue(any(e["agency"] == "bea" and e["period"].endswith("August 2026") for e in current_schedule()["events"]))

    def test_unknown_calendar_year_is_not_invented(self):
        self.assertNotIn(2027, current_schedule()["years"])

    def test_health_never_claims_scheduler_configured_without_runs(self):
        self.assertTrue(all(j["state"] == "pending" for j in update_health()["jobs"]))
        run = MacroMaintenanceRun.objects.create(mode="official", status="success", finished_at=timezone.now() - timedelta(hours=21))
        self.assertEqual(update_health()["jobs"][0]["state"], "warning")
        self.assertEqual(update_health()["jobs"][0]["run"], run)

    def test_read_only_pages_do_not_fetch(self):
        user = get_user_model().objects.create_user(username="macro-health")
        FamilyMember.objects.create(family=Family.objects.create(name="宏观维护测试"), user=user, display_name="成员", role=FamilyMember.ROLE_MEMBER)
        self.client.force_login(user)
        with patch("macro.services.fetch_source", side_effect=AssertionError("GET must not fetch")):
            for name in ["status", "calendar", "sources"]:
                self.assertEqual(self.client.get(reverse("macro:" + name)).status_code, 200)

    def test_command_date_validation_and_nonzero_failure(self):
        with self.assertRaises(CommandError):
            call_command("update_macro", start=date(2025, 1, 1))
        with patch("macro.management.commands.update_macro.maintain", return_value={"failures": [{"group": "pbc"}]}):
            with self.assertRaises(CommandError):
                call_command("update_macro", stdout=StringIO())


class MaintenanceLockTests(TransactionTestCase):
    def test_interrupted_run_is_retained_and_new_run_succeeds(self):
        previous = MacroMaintenanceRun.objects.create(mode="official")
        with patch("macro.maintenance.update_official", return_value={"failures": [], "reports": []}):
            maintain("official", write=True)
        previous.refresh_from_db()
        self.assertEqual(previous.status, "interrupted")
        self.assertEqual(MacroMaintenanceRun.objects.order_by("-pk").first().status, "success")

    def test_competing_session_cannot_acquire_lock(self):
        import psycopg
        from .maintenance import LOCK_ID
        with maintenance_lock(), psycopg.connect(**connection.get_connection_params()) as other:
            self.assertFalse(other.execute("SELECT pg_try_advisory_lock(%s)", [LOCK_ID]).fetchone()[0])
