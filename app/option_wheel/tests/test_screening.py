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
from portfolio.models import InvestmentAccount, InvestmentPosition, InvestmentTransaction, OptionContract, PortfolioSnapshot, Security, TradeTypeChoices
from option_wheel.jobs import job_payload, run_job
from option_wheel.put_quote_jobs import run_job as run_put_quote_job
from option_wheel.put_quote_probe import quote_code
from option_wheel.screening import compare_close_rows, compare_probe_rows, covered_stock, present_results
from option_wheel.models import WheelAnalysisJob, WheelBrokerAccountSnapshot, WheelDecision, WheelPositionReview, WheelPutQuoteJob, WheelWatchItem
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

    def test_home_shows_recorded_us_shares_and_covered_call_threshold(self):
        security = Security.objects.create(symbol="INTC", name="Intel", market="US", asset_type="stock")
        InvestmentPosition.objects.create(account=self.account, security=security, quantity=Decimal("120"),
                                          avg_cost=Decimal("31"), position_date=timezone.localdate())
        response = self.client.get(reverse("option_wheel:index"))
        self.assertContains(response, "120 股")
        self.assertContains(response, "可比较 Covered Call")
        self.assertEqual(WheelBrokerAccountSnapshot.objects.count(), 0)

    def test_existing_short_call_occupies_shares_and_alternatives_are_not_additive(self):
        stock = Security.objects.create(symbol="INTC", name="Intel", market="US", asset_type="stock")
        call = Security.objects.create(symbol="INTC-C", market="US", asset_type="option")
        OptionContract.objects.create(security=call, underlying=stock, option_type="call",
                                      strike_price=Decimal("35"), expiration_date=date(2026, 10, 2))
        InvestmentPosition.objects.create(account=self.account, security=stock, quantity=Decimal("190"),
                                          avg_cost=Decimal("31"), position_date=timezone.localdate())
        InvestmentPosition.objects.create(account=self.account, security=call, quantity=Decimal("-1"),
                                          avg_cost=Decimal("0.5"), position_date=timezone.localdate())
        self.assertEqual(covered_stock(self.family, ["INTC"]), {})
        page = self.client.get(reverse("option_wheel:index"))
        self.assertContains(page, "已被未平仓 Call 覆盖 100 股")
        self.assertContains(page, "还能覆盖 0 张")

    def test_stock_lots_are_only_shown_when_recorded_buys_reconcile(self):
        stock = Security.objects.create(symbol="INTC", name="Intel", market="US", asset_type="stock")
        InvestmentPosition.objects.create(account=self.account, security=stock, quantity=Decimal("200"),
                                          avg_cost=Decimal("31"), position_date=timezone.localdate())
        for quantity, price in (("100", "30"), ("100", "32")):
            InvestmentTransaction.objects.create(
                account=self.account, security=stock, trade_date=timezone.localdate(),
                trade_type=TradeTypeChoices.BUY, quantity=Decimal(quantity),
                price=Decimal(price), amount=Decimal(quantity) * Decimal(price),
            )
        holdings = covered_stock(self.family, ["INTC"])["INTC"]
        self.assertEqual(holdings[0]["available_contracts"], 2)
        self.assertEqual([lot["cost"] for lot in holdings[0]["cost_lots"]], [Decimal("30"), Decimal("32")])
        page = self.client.get(reverse("option_wheel:index"))
        self.assertContains(page, "每股成本 $32.00")

    def test_open_put_page_uses_real_position_and_marks_missing_quotes_unknown(self):
        stock = Security.objects.create(symbol="INTC", name="Intel", market="US", asset_type="stock")
        put = Security.objects.create(symbol="INTC-P", market="US", asset_type="option")
        OptionContract.objects.create(security=put, underlying=stock, option_type="put",
                                      strike_price=Decimal("30"), expiration_date=date(2026, 10, 2))
        InvestmentPosition.objects.create(account=self.account, security=put, quantity=Decimal("-1"),
                                          avg_cost=Decimal("1.5"), current_price=Decimal("0.8"),
                                          unrealized_pnl=Decimal("70"), position_date=timezone.localdate())
        page = self.client.get(reverse("option_wheel:holdings"))
        self.assertContains(page, "管理未平仓 Put")
        self.assertContains(page, "INTC")
        self.assertContains(page, "150.00")
        self.assertContains(page, "未知 / 未知")

        response = self.client.post(reverse("option_wheel:record_put_review"), {
            "position_id": InvestmentPosition.objects.get(security=put).pk,
            "choice": "roll_down_out", "note": "等待 Ask 报价后比较",
        })
        self.assertEqual(response.status_code, 302)
        review = WheelPositionReview.objects.get()
        self.assertEqual(review.frozen_facts["quantity"], "-1.000000")
        self.assertIsNone(review.linked_transaction_id)
        close_trade = InvestmentTransaction.objects.create(
            account=self.account, security=put, trade_date=timezone.localdate(),
            trade_type=TradeTypeChoices.BUY, position_effect=InvestmentTransaction.EFFECT_CLOSE,
            quantity=Decimal("1"), price=Decimal("0.8"), amount=Decimal("80"),
        )
        response = self.client.post(reverse("option_wheel:link_put_transaction", args=[review.pk]), {
            "transaction_id": close_trade.pk,
        })
        self.assertEqual(response.status_code, 302)
        review.refresh_from_db()
        self.assertEqual(review.linked_transaction_id, close_trade.pk)
        self.assertEqual(InvestmentTransaction.objects.count(), 1)

    @patch("option_wheel.put_quote_jobs.fetch_exact_put_quotes")
    def test_open_put_quote_job_saves_vendor_observation_without_portfolio_write(self, fetch):
        stock = Security.objects.create(symbol="INTC", name="Intel", market="US", asset_type="stock")
        put = Security.objects.create(symbol="INTC260925P30000", market="US", asset_type="option")
        OptionContract.objects.create(security=put, underlying=stock, option_type="put",
                                      strike_price=Decimal("30"), expiration_date=date(2026, 9, 25))
        InvestmentPosition.objects.create(account=self.account, security=put, quantity=Decimal("-1"),
                                          avg_cost=Decimal("1.5"), position_date=timezone.localdate())
        job = WheelPutQuoteJob.objects.create(
            family=self.family, requested_by=self.user,
            expires_at=timezone.now() + timedelta(minutes=5),
        )
        fetch.return_value = {"US.INTC260925P30000": {
            "bid": "0.65", "ask": "0.80", "probability": "23.4",
            "as_of": "2026-09-22 09:35:00", "iv": "42", "delta": "-0.2",
        }}
        run_put_quote_job(job.pk)
        job.refresh_from_db()
        self.assertEqual(job.status, "saved")
        fetch.assert_called_once_with(["US.INTC260925P30000"])
        page = self.client.get(reverse("option_wheel:holdings"))
        self.assertContains(page, "$0.65")
        self.assertContains(page, "$0.80")
        self.assertContains(page, "23.4%")
        self.assertEqual(InvestmentTransaction.objects.count(), 0)

    def test_portfolio_occ_symbol_maps_to_unpadded_futu_code(self):
        stock = Security.objects.create(symbol="SPCX", market="US", asset_type="stock")
        put = Security.objects.create(symbol="SPCX260925P00152500", market="US", asset_type="option")
        contract = OptionContract.objects.create(
            security=put, underlying=stock, option_type="put",
            strike_price=Decimal("152.5"), expiration_date=date(2026, 9, 25),
        )
        self.assertEqual(quote_code(contract), "US.SPCX260925P152500")
        contract.is_adjusted = True
        self.assertIsNone(quote_code(contract))

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
        self.assertNotIn("contract_iv_percentile", row)
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
        job.status = "running"
        self.assertIn("Futu 历史收盘数据", job_payload(job)["message"])
        job.status = "queued"
        reference = timezone.now().astimezone(ZoneInfo("America/New_York")).date() - timedelta(days=1)
        fetch.return_value = {"reference_date": str(reference), "symbols": [{
            "symbol": "INTC", "issues": [], "underlying_iv_percentile": "65",
            "underlying_iv_queried_at": "2026-09-22T05:00:00-04:00",
            "contracts": [{"code": "US.INTC-TEST", "strike": "30",
                "size": 100, "close": "1.25", "iv": "42", "probability": "18", "issues": []}],
        }]}
        run_job(job.pk)
        job.refresh_from_db()
        self.assertEqual(job.status, "saved", job.message)
        row = job.screening_results[0]
        self.assertEqual(row["premium"], "125.00")
        self.assertEqual(row["reference_date"], str(reference))
        self.assertIsNone(row["delta"])
        self.assertIsNotNone(row["annualized_premium_rate"])
        self.assertEqual(row["underlying_iv_percentile"], "65.00")
        self.assertNotIn("contract_iv_percentile", row)
        self.assertContains(self.client.get(reverse("option_wheel:job_detail", args=[job.pk])), "收盘参考")
        self.assertEqual(PortfolioSnapshot.objects.count(), 0)

    def test_saved_results_keep_raw_rows_but_both_pages_show_only_matching_puts(self):
        selection = self.selection()
        selection["mode"] = "screening_close_v2"
        selection["symbols"] = ["INTC", "AMD"]
        rows = [
            {"symbol": "INTC", "code": "US.INTC-OUT", "strategy": "PUT", "premium": "90.00", "probability": "5", "risks": []},
            {"symbol": "INTC", "code": "US.INTC-HIGH", "strategy": "PUT", "premium": "150.00", "probability": "30", "risks": []},
            {"symbol": "AMD", "code": "US.AMD-LOW", "strategy": "PUT", "premium": "200.00", "probability": "20", "risks": []},
            {"symbol": "INTC", "code": "US.INTC-LOW", "strategy": "PUT", "premium": "120.00", "probability": "10",
             "risks": ["标的 IV 百分位是 Futu 最新查询值，并非历史收盘日数值"]},
        ]
        job = WheelAnalysisJob.objects.create(
            family=self.family, requested_by=self.user, selection=selection,
            status="saved", screening_results=rows, expires_at=timezone.now() + timedelta(minutes=12),
        )
        displayed = present_results(rows, selection)
        self.assertEqual([row["code"] for row in displayed], ["US.AMD-LOW", "US.INTC-LOW", "US.INTC-HIGH"])
        self.assertEqual(len(job.screening_results), 4)
        self.assertNotIn("标的 IV 百分位是 Futu 最新查询值", displayed[1]["risks"])
        for url in (reverse("option_wheel:index"), reverse("option_wheel:job_detail", args=[job.pk])):
            page = self.client.get(url)
            self.assertContains(page, "US.INTC-LOW")
            self.assertNotContains(page, "US.INTC-OUT")
            self.assertNotContains(page, "<th>到期日</th>", html=False)
            self.assertNotContains(page, "标的 IV 百分位是 Futu 最新查询值")
            self.assertContains(page, "规则比较")

    def test_close_screen_does_not_pad_with_out_of_range_puts(self):
        selection = self.selection()
        report = {"reference_date": selection["analysis_date"], "symbols": [{
            "symbol": "INTC", "contracts": [
                {"code": "US.INTC-LOW", "strategy": "PUT", "strike": "30", "size": 100,
                 "close": "0.90", "iv": "40", "probability": "10", "issues": []},
                {"code": "US.INTC-OK", "strategy": "PUT", "strike": "31", "size": 100,
                 "close": "1.25", "iv": "41", "probability": "20", "issues": []},
            ],
        }]}
        rows = compare_close_rows(report, selection, {}, {})
        self.assertEqual([row["code"] for row in rows], ["US.INTC-OK"])

    def test_close_call_is_compared_for_each_account_with_100_shares(self):
        selection = self.selection()
        report = {"reference_date": selection["analysis_date"], "symbols": [{
            "symbol": "INTC", "contracts": [{"code": "US.INTC-C1", "strategy": "CALL", "strike": "32",
                                          "size": 100, "close": "0.50", "iv": "40", "probability": "25", "issues": []}],
        }]}
        holdings = {"INTC": [{"account": "盈透证券", "shares": Decimal(120), "cost": Decimal(31)},
                             {"account": "致富证券（公户）", "shares": Decimal(100), "cost": Decimal(34)}]}
        rows = compare_close_rows(report, selection, {}, holdings)
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["account"] for row in rows}, {"盈透证券", "致富证券（公户）"})
        self.assertEqual({row["break_even"] for row in rows}, {"30.50", "33.50"})
        self.assertTrue(any("低于该账户持股成本" in risk for row in rows for risk in row["risks"]))

    def test_live_call_uses_each_account_cost_without_put_premium_gate(self):
        selection = self.selection()
        holdings = {"INTC": [{"account": "盈透证券", "shares": Decimal(120), "cost": Decimal(31)},
                             {"account": "致富证券（公户）", "shares": Decimal(100), "cost": Decimal(34)}]}
        probe = [{"symbol": "US.INTC", "market_state": {"market_us": "MORNING"},
                  "representative_contracts": [{
                      "code": "US.INTC-C1", "option_type": "CALL", "strike_time": selection["target_expiration"],
                      "strike_price": "32", "lot_size": 100, "contract_identity_status": "ok",
                      "dynamic_quote": {"bid_price": {"value": "0.50"}, "contract_size": {"value": 100}},
                      "analytics": {"probability": {"fields": {"strike_probability": {"value": "25"}}}},
                  }]}]
        rows = compare_probe_rows(probe, selection, holdings, {})
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["break_even"] for row in rows}, {"30.50", "33.50"})
        self.assertEqual({row["premium"] for row in rows}, {"50.00"})

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
