from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core import signing
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from family_core.models import ExchangeRate, Family, FamilyMember
from ledger.models import BankAccount
from portfolio.models import InvestmentAccount, PortfolioSnapshot
from option_wheel.jobs import run_job
from option_wheel.models import WheelAnalysisJob, WheelBrokerAccountSnapshot, WheelDecision, WheelWatchItem
from option_wheel.watch_refresh import refresh_watch_events


class ScreeningTests(TestCase):
    def setUp(self):
        self.family = Family.objects.create(name="screening family")
        self.user = get_user_model().objects.create_user(username="screen-admin", is_superuser=True, is_staff=True)
        self.member = FamilyMember.objects.create(family=self.family, user=self.user, display_name="我")
        self.bank = BankAccount.objects.create(family=self.family, member=self.member, account_name="盈透证券", supports_investment=True)
        self.account = InvestmentAccount.objects.create(bank_account=self.bank)
        self.watch = WheelWatchItem.objects.create(family=self.family, symbol="INTC", name="Intel")
        self.client.force_login(self.user)

    def selection(self):
        today = timezone.now().astimezone(ZoneInfo("America/New_York")).date()
        friday = today + timedelta(days=(4 - today.weekday()) % 7 or 7)
        return {
            "mode": "screening_v2", "symbols": ["INTC"],
            "analysis_date": today.isoformat(), "target_expiration": friday.isoformat(),
            "premium_min": "100", "premium_max": "500",
            "allow_earnings": False, "allow_dividend": False,
        }

    def test_home_reads_latest_portfolio_cash_without_capacity_snapshot(self):
        snapshot_date = timezone.localdate()
        ExchangeRate.objects.create(base_currency="USD", quote_currency="CNY", rate=Decimal("7.2"), rate_date=snapshot_date)
        PortfolioSnapshot.objects.create(
            family=self.family, member=self.member, account=self.account,
            snapshot_date=snapshot_date, total_cash=Decimal("7200"),
            total_asset=Decimal("14400"), currency="CNY", extra_data={"complete": True},
        )
        response = self.client.get(reverse("option_wheel:index"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "1000.00")
        self.assertContains(response, "2000.00")
        self.assertContains(response, "合约对比")
        self.assertNotContains(response, "容量快照")
        self.assertEqual(WheelBrokerAccountSnapshot.objects.count(), 0)

    @patch("option_wheel.jobs.launch_job")
    def test_submit_is_account_independent_and_idempotent(self, launch):
        token = signing.dumps({"family": self.family.pk, "key": str(uuid4())}, salt="wheel-live-job-v1")
        payload = {"symbols": ["INTC"], "request_token": token, "expiry_choice": "next", "premium_min": "100", "premium_max": "500"}
        with self.captureOnCommitCallbacks(execute=True):
            first = self.client.post(reverse("option_wheel:analyze"), payload, HTTP_ACCEPT="application/json")
            second = self.client.post(reverse("option_wheel:analyze"), payload, HTTP_ACCEPT="application/json")
        self.assertEqual(first.status_code, 202)
        self.assertEqual(first.json()["id"], second.json()["id"])
        self.assertEqual(WheelAnalysisJob.objects.count(), 1)
        self.assertEqual(WheelAnalysisJob.objects.get().selection["account_names"], [])
        self.assertEqual(WheelBrokerAccountSnapshot.objects.count(), 0)
        launch.assert_called_once()

    @patch("option_wheel.jobs.fetch_probe")
    def test_run_saves_comparable_put_without_portfolio_writes(self, fetch):
        selection = self.selection()
        self.watch.events_checked_at = timezone.now()
        self.watch.events_covered_until = timezone.now().date() + timedelta(days=35)
        self.watch.save()
        job = WheelAnalysisJob.objects.create(
            family=self.family, requested_by=self.user, selection=selection,
            expires_at=timezone.now() + timedelta(minutes=12),
        )
        fetch.return_value = [{
            "symbol": "US.INTC", "underlying_iv": {"iv_percentile": 65, "source": "futu_get_option_underlying_overview"},
            "representative_contracts": [{
                "code": "US.INTC-P1", "option_type": "PUT", "strike_time": selection["target_expiration"],
                "strike_price": "30", "dynamic_quote": {
                    "bid_price": {"value": "1.5"}, "contract_size": {"value": 100},
                    "delta": {"value": "-0.18"}, "implied_volatility": {"value": "42"},
                },
                "analytics": {"probability": {"fields": {"strike_probability": {"value": "18"}}}},
            }],
        }]
        run_job(job.pk)
        job.refresh_from_db()
        self.assertEqual(job.status, "saved")
        row = job.screening_results[0]
        self.assertEqual(row["premium"], "150.00")
        self.assertEqual(row["break_even"], "28.50")
        self.assertEqual(row["delta"], "-0.1800")
        self.assertEqual(row["underlying_iv_percentile"], "65.00")
        self.assertIsNone(row["contract_iv_percentile"])
        self.assertEqual(WheelDecision.objects.count(), 0)
        self.assertEqual(PortfolioSnapshot.objects.count(), 0)
        self.assertContains(self.client.get(reverse("option_wheel:job_detail", args=[job.pk])), "28.50")

    def test_price_refresh_does_not_change_events(self):
        self.watch.next_earnings = timezone.now().date() + timedelta(days=10)
        self.watch.save()
        with patch("option_wheel.watch_refresh.get_futu_market_snapshots", return_value={
            "US.INTC": {"last_price": "31.25", "quote_time": "2026-09-22 10:00:00"},
        }):
            response = self.client.post(reverse("option_wheel:watch_action"), {"action": "refresh_prices"})
        self.assertEqual(response.status_code, 302)
        self.watch.refresh_from_db()
        self.assertEqual(self.watch.price, Decimal("31.25"))
        self.assertIsNotNone(self.watch.price_as_of)
        self.assertEqual(self.watch.next_earnings, timezone.now().date() + timedelta(days=10))

    @patch("option_wheel.jobs.launch_job")
    def test_submit_previous_close_mode(self, launch):
        token = signing.dumps({"family": self.family.pk, "key": str(uuid4())}, salt="wheel-live-job-v1")
        payload = {"symbols": ["INTC"], "request_token": token, "expiry_choice": "next",
                   "premium_min": "100", "premium_max": "500", "analysis_basis": "close"}
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse("option_wheel:analyze"), payload, HTTP_ACCEPT="application/json")
        self.assertEqual(response.status_code, 202)
        self.assertEqual(WheelAnalysisJob.objects.get().selection["mode"], "screening_close_v2")
        launch.assert_called_once()

    @patch("option_wheel.screen_close.fetch")
    def test_previous_close_result_is_labelled_and_does_not_invent_delta(self, fetch):
        selection = self.selection()
        selection["mode"] = "screening_close_v2"
        self.watch.events_checked_at = timezone.now()
        self.watch.events_covered_until = timezone.now().date() + timedelta(days=35)
        self.watch.save()
        job = WheelAnalysisJob.objects.create(
            family=self.family, requested_by=self.user, selection=selection,
            expires_at=timezone.now() + timedelta(minutes=12),
        )
        reference = timezone.now().astimezone(ZoneInfo("America/New_York")).date() - timedelta(days=1)
        fetch.return_value = {"reference_date": str(reference), "symbols": [{
            "symbol": "INTC", "issues": [], "contracts": [{"code": "US.INTC-TEST", "strike": "30",
                "size": 100, "close": "1.25", "iv": "42", "probability": "18", "issues": []}],
        }]}
        run_job(job.pk)
        job.refresh_from_db()
        self.assertEqual(job.status, "saved", job.message)
        row = job.screening_results[0]
        self.assertEqual(row["premium"], "125.00")
        self.assertEqual(row["reference_date"], str(reference))
        self.assertIsNone(row["delta"])
        self.assertIsNone(row["annualized_premium_rate"])
        self.assertContains(self.client.get(reverse("option_wheel:job_detail", args=[job.pk])), "收盘参考")
        self.assertEqual(PortfolioSnapshot.objects.count(), 0)

    @patch("option_wheel.watch_refresh._fetch_dividend_calendar", return_value=({"status": "ok"}, []))
    @patch("option_wheel.watch_refresh.sdk_call")
    @patch("futu.OpenQuoteContext")
    def test_daily_calendar_uses_supported_weekly_windows(self, context, sdk_call, dividends):
        today = timezone.now().astimezone(ZoneInfo("America/New_York")).date()
        sdk_call.return_value = {"status": "ok", "data": [
            {"security": "US.INTC", "earnings_date": str(today + timedelta(days=14))},
        ]}
        refresh_watch_events(self.family)
        self.watch.refresh_from_db()
        self.assertEqual(self.watch.next_earnings, today + timedelta(days=14))
        self.assertEqual(self.watch.events_covered_until, today + timedelta(days=35))
        self.assertEqual(len(sdk_call.call_args_list), 6)
        for call in sdk_call.call_args_list:
            begin = date.fromisoformat(call.kwargs["begin_date"])
            end = date.fromisoformat(call.kwargs["end_date"])
            self.assertLessEqual((end - begin).days, 6)
        self.assertEqual(dividends.call_count, 36)
