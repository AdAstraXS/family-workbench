from datetime import date, timedelta
from decimal import Decimal
from tempfile import TemporaryDirectory
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from family_core.models import Family, FamilyMember
from .adapters import SourceError
from .alerts import expected_period, freshness_issues, synchronize_alerts
from .calendar_maintenance import parse_ism_calendar, parse_ics
from .fetch_worker import ISMPublicSession
from .models import MacroAlert, MacroAlertRead, MacroObservation, MacroObservationRevision, MacroPublication
from .nas_operations import backup_inventory, ingest
from .presentation import chart_reference
from .publications import annotate_publications, bls_publication, latest_ism_links, store_publication
from .registry import SERIES
from .services import import_group


def payload():
    return {"url": "https://fred.stlouisfed.org/graph/fredgraph.csv?id=UNRATE",
            "text": "observation_date,UNRATE\n2026-09-01,4.3\n"}


def publication():
    return {"agency": "bls", "country": "US", "key": "employment", "title": "Employment Situation",
            "period_date": date(2026, 9, 1), "release_date": date(2026, 10, 2), "codes": ["UNRATE"],
            "source_url": "https://www.bls.gov/news.release/empsit.nr0.htm", "source_hash": "a" * 64,
            "evidence": {"header": "embargoed until October 2, 2026"}}


class AlertParserTests(SimpleTestCase):
    def test_public_source_errors_show_status_without_worker_output(self):
        import subprocess
        from .services import fetch_page
        for label, expected in [('HTTP_403', 'HTTP_403'), ('private-worker-output', '响应异常')]:
            failure = subprocess.CalledProcessError(1, [], output='{"error":"' + label + '"}')
            with patch('macro.services.subprocess.run', side_effect=failure):
                with self.assertRaises(SourceError) as caught:
                    fetch_page('https://www.bls.gov/news.release/empsit.nr0.htm')
                self.assertIn(expected, str(caught.exception))
                self.assertNotIn('private-worker-output', str(caught.exception))

    def test_bls_monthly_expectation_does_not_require_a_title_period(self):
        spec = next(s for s in SERIES if s.code == 'PAYEMS')
        event = {'agency':'bls', 'period':'Employment Situation', 'day':date(2026, 10, 2)}
        self.assertEqual(expected_period(event, spec), date(2026, 9, 1))

    def test_bls_local_timezone_alias_observes_daylight_saving(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        for month, hour in [(1, 21), (7, 20)]:
            text = f'BEGIN:VCALENDAR\nBEGIN:VEVENT\nSUMMARY:Employment Situation\nDTSTART;TZID=US-Eastern:2026{month:02}02T083000\nEND:VEVENT\nEND:VCALENDAR'
            event = parse_ics(text, 'bls')[0]
            when = datetime.fromisoformat(event['date'] + 'T' + event['time']).replace(tzinfo=ZoneInfo(event['timezone']))
            self.assertEqual(when.astimezone(ZoneInfo('Asia/Shanghai')).hour, hour)

    def test_ism_anonymous_redirect_rejects_other_hosts_and_ports(self):
        from urllib.request import Request
        handler = ISMPublicSession()
        request = Request('https://www.ismworld.org/public-report/')
        for url in ['https://evil.org/', 'https://www.ismworld.org:8443/', 'https://ecommerce.ismworld.org/other/']:
            with self.assertRaises(ValueError):
                handler.redirect_request(request, None, 302, '', {}, url)

    def test_reference_lines_cover_raw_changes_and_nonfarm_additions(self):
        by_code = {s.code: s for s in SERIES}
        for code in ["CPI_YOY", "CPI_MOM", "PPI_YOY", "M2_YOY", "FAI_CUM_YOY", "HOUSEHOLD_LOANS_CUM", "TSF_CUM", "GOVERNMENT_BONDS_CUM"]:
            self.assertEqual(chart_reference(by_code[code], "level"), "0")
        self.assertEqual(chart_reference(by_code["PAYEMS"], "change"), "0")
        self.assertEqual(chart_reference(by_code["HOUSE_NEW_MOM"], "level"), "100")
        self.assertEqual(chart_reference(by_code["PMI_MANUFACTURING"], "level"), "50")
        self.assertIsNone(chart_reference(by_code["M2"], "level"))

    def test_bls_uses_embargo_timestamp_and_reference_period(self):
        item = bls_publication('<title>Employment Situation Summary - 2026 M09 Results</title>'
            '<pre>Transmission of material is embargoed until 8:30 a.m. (ET) Friday, October 2, 2026</pre>',
            "employment", publication()["source_url"], ["UNRATE"])
        self.assertEqual(item["period_date"], date(2026, 9, 1))
        self.assertEqual(item["release_date"], date(2026, 10, 2))
        with self.assertRaises(SourceError):
            bls_publication('<title>2026 M09 Results</title><pre>Calendar October 2, 2026</pre>', "employment", "", [])

    def test_ism_discovery_and_calendar_are_separate_from_actual_dates(self):
        prefix = "/supply-management-news-and-reports/reports/ism-pmi-reports/"
        html = f'<a href="{prefix}pmi/september/">View Report</a><a href="{prefix}services/august/">View Report</a>'
        self.assertEqual(len(latest_ism_links(html)), 2)
        with self.assertRaises(SourceError):
            latest_ism_links(html.replace(prefix + "pmi", "https://evil.org/pmi", 1))
        events = parse_ism_calendar('<h3>2026 ISM Reports Release Dates</h3><table><tr><td>January 2026</td><td>5</td><td>7</td></tr>'
            '<tr><td>July 2026</td><td>1</td><td>6**</td></tr></table>')
        self.assertEqual(events[0]["period"], "December 2025")
        self.assertEqual(events[-1]["date"], "2026-07-06")
        self.assertEqual(events[-1]["timezone"], "America/New_York")

    def test_backup_inventory_preserves_first_and_monthly_points(self):
        import os
        now = timezone.now()
        with TemporaryDirectory() as root:
            for number, days in enumerate([65, 64, 40, 39, 2]):
                path = Path(root) / f"family-workbench-macro-official-20260101-00000{number}.dump"
                path.write_bytes(b"backup")
                stamp = (now - timedelta(days=days)).timestamp()
                os.utime(path, (stamp, stamp))
            Path(root, "family-workbench-macro-mapping-compat-20261005.dump").write_bytes(b"protected")
            report = backup_inventory(root, now)
            self.assertEqual(report["count"], 5)
            self.assertFalse(report["deletion_enabled"])
            self.assertNotIn("family-workbench-macro-official-20260101-000000.dump", [p["name"] for p in report["candidates"]])
            self.assertEqual(len(list(Path(root).glob("*.dump"))), 6)


class AlertServiceTests(TestCase):
    def test_release_date_annotation_keeps_value_evidence_and_survives_csv_refresh(self):
        import_group("fred_UNRATE", write=True, fetcher=lambda *_: payload())
        store_publication(publication())
        self.assertEqual(annotate_publications(), {"revised": 1})
        point = MacroObservation.objects.get()
        self.assertEqual(point.value, Decimal("4.3"))
        self.assertEqual(point.release_date, date(2026, 10, 2))
        self.assertIsNone(point.revisions.get(number=1).release_date)
        self.assertEqual(point.revisions.get(number=2).evidence["publication"]["hash"], "a" * 64)
        self.assertEqual(annotate_publications(), {"revised": 0})
        self.assertEqual(import_group("fred_UNRATE", write=True, fetcher=lambda *_: payload())["unchanged"], 1)
        self.assertEqual(MacroObservationRevision.objects.count(), 2)
        with self.assertRaises(SourceError):
            store_publication({**publication(), "release_date": timezone.localdate() + timedelta(days=1)})

    def test_published_but_missing_data_alert_ignores_null_rows(self):
        item = store_publication(publication())
        MacroPublication.objects.filter(pk=item.pk).update(first_seen_at=timezone.now() - timedelta(days=3))
        store_publication(publication())  # Rechecking must not restart the grace period.
        with patch("macro.alerts.current_schedule", return_value={"events": []}):
            self.assertEqual(len(freshness_issues()), 1)
            import_group("fred_UNRATE", write=True, fetcher=lambda *_: {**payload(), "text": payload()["text"].replace("4.3", ".") + "2026-08-01,4.2\n"})
            self.assertEqual(len(freshness_issues()), 1)
            import_group("fred_UNRATE", write=True, fetcher=lambda *_: payload())
            self.assertEqual(freshness_issues(), [])

    def test_notifications_deduplicate_resolve_and_reopen(self):
        issue = {"key": "late:US:UNRATE", "title": "漏更", "message": "需检查", "details": {}}
        with patch("macro.alerts.freshness_issues", return_value=[issue]), patch("macro.alerts.update_health", return_value={"jobs": []}):
            synchronize_alerts(); synchronize_alerts()
        self.assertEqual(MacroAlert.objects.count(), 1)
        with patch("macro.alerts.freshness_issues", return_value=[]), patch("macro.alerts.update_health", return_value={"jobs": []}):
            synchronize_alerts()
        self.assertIsNotNone(MacroAlert.objects.get().resolved_at)
        with patch("macro.alerts.freshness_issues", return_value=[issue]), patch("macro.alerts.update_health", return_value={"jobs": []}):
            synchronize_alerts()
        self.assertEqual(MacroAlert.objects.count(), 2)

    def test_site_notification_read_is_personal_and_get_is_read_only(self):
        family = Family.objects.create(name="通知测试")
        users = [get_user_model().objects.create_user(username=name) for name in ["reader-a", "reader-b"]]
        for user in users:
            FamilyMember.objects.create(family=family, user=user, display_name=user.username, role=FamilyMember.ROLE_VIEWER)
        alert = MacroAlert.objects.create(key="test", title="测试提醒", message="测试", last_seen_at=timezone.now())
        self.client.force_login(users[0])
        with patch("macro.services.fetch_page", side_effect=AssertionError("no GET network")):
            self.assertContains(self.client.get(reverse("macro:status")), "站内通知")
        self.assertEqual(MacroAlertRead.objects.count(), 0)
        self.assertEqual(self.client.post(reverse("macro:read_alert", args=[alert.pk])).status_code, 302)
        self.assertEqual(MacroAlertRead.objects.get().user, users[0])
        self.client.force_login(users[1])
        self.assertContains(self.client.get(reverse("macro:status")), "1 项待查看提醒")
        self.assertEqual(self.client.post(reverse("macro:status")).status_code, 403)

    def test_host_evidence_rejects_other_tasks_and_delete_candidates(self):
        with self.assertRaises(ValueError):
            ingest({"tasks": [{"id": 1}]}, timezone.now())
