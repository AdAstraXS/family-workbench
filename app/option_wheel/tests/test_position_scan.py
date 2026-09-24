from datetime import date, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from family_core.models import Family, FamilyMember
from ledger.models import BankAccount
from portfolio.models import (
    InvestmentAccount, InvestmentPosition, InvestmentTransaction, OptionContract,
    Security, TradeStatusChoices, TradeTypeChoices,
)
from option_wheel.models import WheelPositionScanJob
from option_wheel.position_scan import comparison_rows, fetch_position_scan, select_chain_rows
from option_wheel.position_scan_jobs import run_job
from option_wheel.position_summary import option_position_rows
from option_wheel.put_quote_probe import PutQuoteError


class PositionScanTests(TestCase):
    def setUp(self):
        self.family = Family.objects.create(name="position family")
        self.user = get_user_model().objects.create_user(username="position-admin", is_superuser=True, is_staff=True)
        self.member = FamilyMember.objects.create(family=self.family, user=self.user, display_name="我")
        bank = BankAccount.objects.create(family=self.family, member=self.member, account_name="盈透证券", supports_investment=True)
        self.account = InvestmentAccount.objects.create(bank_account=bank)
        self.stock = Security.objects.create(symbol="AMD", name="AMD", market="US", asset_type="stock")
        self.security = Security.objects.create(symbol="AMD-P", market="US", asset_type="option")
        today = timezone.now().astimezone(ZoneInfo("America/New_York")).date()
        self.expiry = today + timedelta(days=70)
        self.contract = OptionContract.objects.create(
            security=self.security, underlying=self.stock, option_type="put",
            strike_price=Decimal("150"), expiration_date=self.expiry,
        )
        self.position = InvestmentPosition.objects.create(
            account=self.account, security=self.security, quantity=Decimal("2"),
            avg_cost=Decimal("11.20"), position_date=today,
        )
        self.client.force_login(self.user)

    def test_home_and_detail_use_recorded_purpose_and_per_contract_amounts(self):
        InvestmentTransaction.objects.create(
            account=self.account, security=self.security, trade_date=self.position.position_date,
            trade_type=TradeTypeChoices.BUY, position_effect=InvestmentTransaction.EFFECT_OPEN,
            quantity=Decimal("2"), price=Decimal("11.20"), amount=Decimal("2240"),
            option_purpose="protective_put", status=TradeStatusChoices.COMPLETED,
        )
        row = option_position_rows(self.family)[0]
        self.assertEqual(row["purpose"], "protective_put")
        self.assertEqual(row["open_per_contract"], Decimal("1120.00"))
        page = self.client.get(reverse("option_wheel:index"))
        self.assertContains(page, "已录入期权持仓")
        self.assertContains(page, "保护已有正股")
        detail = self.client.get(reverse("option_wheel:position_detail", args=[self.position.pk]))
        self.assertContains(detail, "原仓买入成本")
        self.assertContains(detail, "$1120.00 / 张")
        self.assertContains(detail, "覆盖关系尚待核实")
        form_page = self.client.get(reverse("portfolio:transaction_create"), {"account": self.account.pk})
        self.assertContains(form_page, "期权用途 / 策略归属")
        self.assertContains(form_page, "车轮策略 · 卖出 Put")

    def test_legacy_position_remains_unclassified_and_other_family_cannot_open(self):
        self.assertEqual(option_position_rows(self.family)[0]["purpose_label"], "待归类")
        other = Family.objects.create(name="other family")
        other_user = get_user_model().objects.create_user(username="other-position")
        FamilyMember.objects.create(family=other, user=other_user, display_name="其他人")
        self.client.force_login(other_user)
        self.assertEqual(self.client.get(reverse("option_wheel:position_detail", args=[self.position.pk])).status_code, 404)

    def test_comparison_keeps_per_contract_cash_and_old_profit_separate(self):
        row = option_position_rows(self.family)[0]
        result = {"old_quote": {"bid": "9.80", "as_of": "2026-09-22 10:00:00"},
                  "candidates": [{"code": "US.AMD270319P150000", "strike": "150",
                                  "quote": {"ask": "13.40", "iv": "45", "delta": "-0.3"}}]}
        compared = comparison_rows(row, result)
        self.assertEqual(compared["old_close"], Decimal("980.00"))
        self.assertEqual(compared["old_pnl"], Decimal("-140.00"))
        self.assertEqual(compared["rows"][0]["new_open"], Decimal("1340.00"))
        self.assertEqual(compared["rows"][0]["net"], Decimal("-360.00"))
        self.assertIsNone(comparison_rows(row, {"old_quote": {}, "candidates": result["candidates"]})["rows"][0]["net"])

    def test_saved_previous_day_quotes_are_labelled_as_references(self):
        queried_at = datetime(2026, 9, 23, 12, tzinfo=ZoneInfo("America/New_York"))
        self.position.refresh_from_db()
        job = WheelPositionScanJob.objects.create(
            family=self.family, position=self.position, requested_by=self.user,
            target_expiration=self.expiry + timedelta(days=7), status="saved",
            started_at=queried_at, expires_at=timezone.now() + timedelta(minutes=8),
            result={
                "old_quote": {"bid": "9.80", "as_of": "2026-09-22 15:59:55"},
                "candidates": [{"code": "US.AMD270319P150000", "strike": "150",
                                "quote": {"ask": "13.40", "as_of": "2026-09-22 15:45:40"}}],
                "position_quantity": str(self.position.quantity),
                "position_avg_cost": str(self.position.avg_cost),
                "position_date": self.position.position_date.isoformat(),
            },
        )
        page = self.client.get(reverse("option_wheel:position_detail", args=[job.position_id]))
        self.assertContains(page, "历史报价参考")
        self.assertContains(page, "不是当前可成交报价")
        self.assertContains(page, "报价时间相差超过 1 分钟")
        self.assertContains(page, "参考净支出 $360.00")

    def test_chain_selects_only_standard_puts_of_the_selected_date(self):
        standard = {"option_type": "PUT", "strike_price": "150", "code": "US.AMD270319P150000",
                    "option_standard_type": "STANDARD", "lot_size": 100}
        rows = [standard, {**standard, "code": "US.AMD270319P160000", "strike_price": "160"},
                {**standard, "code": "US.AMD270319C150000", "option_type": "CALL"},
                {**standard, "code": "US.AMD270319P140000", "strike_price": "140", "option_standard_type": "NON_STANDARD"}]
        self.assertEqual([item["strike"] for item in select_chain_rows(rows, self.contract, "short_put")], ["150"])

    @patch("option_wheel.position_scan.fetch_exact_option_quotes")
    @patch("option_wheel.position_scan.sdk_call_with_timeout_retry")
    @patch("futu.OpenQuoteContext")
    def test_futu_scan_reads_only_selected_expiry_and_bounded_quotes(self, context, chain, quotes):
        target = self.expiry + timedelta(days=7)
        row = {"strike_time": target.isoformat(), "option_type": "PUT", "strike_price": "150",
               "code": f"US.AMD{target:%y%m%d}P150000", "option_standard_type": "STANDARD", "lot_size": 100}
        chain.return_value = {"status": "ok", "data": [row, {**row, "strike_time": self.expiry.isoformat()}]}
        quotes.return_value = {}
        result = fetch_position_scan(self.position, target)
        self.assertEqual(result["chain_count"], 1)
        self.assertEqual(result["sample_count"], 1)
        self.assertEqual(result["candidates"][0]["code"], row["code"])
        self.assertEqual(len(quotes.call_args.args[0]), 2)
        self.assertEqual(chain.call_args.kwargs["option_type"], "PUT")
        context.return_value.close.assert_called_once()

    @patch("option_wheel.position_scan.sdk_call_with_timeout_retry")
    @patch("futu.OpenQuoteContext")
    def test_chain_failure_explains_known_reason_without_provider_text(self, context, chain):
        chain.return_value = {
            "status": "error", "category": "provider_error",
            "error": "timeout account=private-account-token",
        }
        with self.assertRaises(PutQuoteError) as raised:
            fetch_position_scan(self.position, self.expiry + timedelta(days=7))
        self.assertIn("服务提示超时", str(raised.exception))
        self.assertNotIn("private-account-token", str(raised.exception))
        context.return_value.close.assert_called_once()

    @patch("option_wheel.position_scan_jobs.launch_job")
    def test_scan_post_queues_one_selected_date_without_portfolio_write(self, launch):
        target = self.expiry + timedelta(days=7)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse("option_wheel:scan_position", args=[self.position.pk]), {
                "target_expiration": target.isoformat(),
            })
        self.assertEqual(response.status_code, 302)
        job = WheelPositionScanJob.objects.get()
        self.assertEqual(job.target_expiration, target)
        self.assertEqual(job.position_id, self.position.pk)
        self.assertEqual(InvestmentTransaction.objects.count(), 0)
        launch.assert_called_once_with(job.pk)
        self.assertEqual(self.client.post(reverse("option_wheel:scan_position", args=[self.position.pk]), {
            "target_expiration": "bad-date",
        }).status_code, 400)

    @patch("option_wheel.position_scan_jobs.fetch_position_scan")
    def test_job_saves_read_only_result(self, fetch):
        job = WheelPositionScanJob.objects.create(
            family=self.family, position=self.position, requested_by=self.user,
            target_expiration=self.expiry + timedelta(days=7),
            expires_at=timezone.now() + timedelta(minutes=8),
        )
        self.position.refresh_from_db()
        fetch.return_value = {"sample_count": 1, "candidates": [], "old_quote": {},
                              "position_quantity": str(self.position.quantity),
                              "position_avg_cost": str(self.position.avg_cost),
                              "position_date": self.position.position_date.isoformat()}
        run_job(job.pk)
        job.refresh_from_db()
        self.assertEqual(job.status, "saved")
        self.assertEqual(InvestmentTransaction.objects.count(), 0)
        self.assertContains(self.client.get(reverse("option_wheel:position_detail", args=[self.position.pk])), "所选到期日")
        self.position.quantity = Decimal("1")
        self.position.save()
        self.assertContains(self.client.get(reverse("option_wheel:position_detail", args=[self.position.pk])), "已变化")
