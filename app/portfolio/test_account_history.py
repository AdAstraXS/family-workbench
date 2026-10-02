from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from family_core.models import Currency, ExchangeRate, Family, FamilyMember
from ledger.models import AssetBalanceSnapshot
from .account_history import SnapshotRates, snapshot_account_data
from .models import InvestmentPosition, InvestmentTransaction, PortfolioSnapshot, PortfolioSnapshotPositionLine, Security
from .tests import create_broker_investment_account


class HistoricalAccountTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="history-member")
        self.family = Family.objects.create(name="历史测试家庭", base_currency="CNY")
        self.member = FamilyMember.objects.create(family=self.family, user=self.user, display_name="我")
        self.account = create_broker_investment_account(self.family, self.member, "历史证券账户")
        for code in ("CNY", "HKD", "USD"):
            Currency.objects.update_or_create(code=code, defaults={"name": code, "is_active": True})
        self.security = Security.objects.create(symbol="HISTORY", name="历史持仓", currency="USD", market="US")
        self.snapshot = PortfolioSnapshot.objects.create(
            family=self.family, snapshot_date=date(2025, 12, 31), currency="CNY",
            total_asset=Decimal("840"), total_cash=Decimal("700"), total_market_value=Decimal("140"),
            extra_data={"complete": True},
        )
        self.cash = PortfolioSnapshotPositionLine.objects.create(
            snapshot=self.snapshot, account=self.account, asset_type="cash", currency="USD",
            asset_name="USD 现金", market_value_original=100, market_value=700, fx_rate=7,
        )
        self.holding = PortfolioSnapshotPositionLine.objects.create(
            snapshot=self.snapshot, account=self.account, security=self.security,
            asset_type="stock", asset_name="历史持仓", currency="USD", quantity=2, price=10,
            price_as_of=date(2025, 12, 31), market_value_original=20, market_value=140,
            cost_original=16, cost=112, unrealized_pnl=28, fx_rate=7,
        )
        for year, pnl in ((2024, 3), (2025, 5), (2026, 90)):
            InvestmentTransaction.objects.create(
                account=self.account, security=self.security, trade_date=date(year, 6, 1),
                trade_type="dividend", currency="USD", realized_pnl=pnl,
            )
        self.client.force_login(self.user)

    def page(self, currency="CNY", year="2025"):
        return self.client.get(reverse("portfolio:account_list"), {"year": year, "currency": currency})

    def test_saved_rates_recover_cumulative_profit_without_global_rates(self):
        self.assertFalse(ExchangeRate.objects.filter(rate_date__lte=date(2025, 12, 31)).exists())
        response = self.page()
        row = response.context["account_rows"][0]
        self.assertEqual(row["realized"], Decimal("56"))
        self.assertEqual(row["unrealized"], Decimal("28"))
        self.assertEqual(row["total_asset"], Decimal("840"))
        self.assertIsNone(row["today_pnl"])

    def test_currency_switch_uses_same_snapshot_bridge(self):
        self.cash.currency = "HKD"
        self.cash.fx_rate = Decimal("0.9")
        self.cash.market_value_original = Decimal("777.7778")
        self.cash.save()
        response = self.page("HKD")
        row = response.context["account_rows"][0]
        self.assertEqual(row["realized"], Decimal("56") / Decimal("0.9"))
        self.assertEqual(row["total_asset"].quantize(Decimal("0.01")), Decimal("933.33"))
        self.assertEqual(response.context["historical_positions"][0]["realized"], Decimal("8"))

    def test_missing_profit_rate_propagates_to_summary_not_zero(self):
        InvestmentTransaction.objects.create(
            account=self.account, trade_date=date(2025, 8, 1), trade_type="dividend",
            currency="EUR", realized_pnl=12,
        )
        response = self.page()
        self.assertIsNone(response.context["account_rows"][0]["realized"])
        self.assertIsNone(response.context["summary_rows"][0]["realized"])
        self.assertContains(response, "EUR/CNY")
        self.assertEqual(response.context["account_rows"][0]["total_asset"], 840)

    def test_only_effective_transactions_contribute_profit(self):
        for status, pnl in (("planned", 100), ("submitted", 200), ("cancelled", 300), ("partial", 2)):
            InvestmentTransaction.objects.create(
                account=self.account, security=self.security, trade_date=date(2025, 8, 1),
                trade_type="sell", status=status, currency="USD", realized_pnl=pnl,
            )
        self.assertEqual(self.page().context["account_rows"][0]["realized"], 70)

    def test_history_get_does_not_write_financial_data(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        with CaptureQueriesContext(connection) as queries:
            self.page()
        writes = [query["sql"] for query in queries if query["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))]
        self.assertEqual(writes, [])

    def test_conflicting_snapshot_rates_are_not_silently_selected(self):
        self.holding.fx_rate = Decimal("8")
        self.holding.save()
        response = self.page()
        self.assertIsNone(response.context["account_rows"][0]["realized"])
        self.assertContains(response, "USD/CNY")

    def test_same_day_formal_ledger_supplies_currency_absent_from_lines(self):
        AssetBalanceSnapshot.objects.create(
            family=self.family, snapshot_date=self.snapshot.snapshot_date, base_currency="CNY",
            usd_to_base=7, hkd_to_base=Decimal("0.9"), is_draft=False,
        )
        row = self.page("HKD").context["account_rows"][0]
        self.assertEqual(row["realized"], Decimal("56") / Decimal("0.9"))

    def test_future_rate_does_not_backfill_history(self):
        ExchangeRate.objects.create(base_currency="EUR", quote_currency="CNY", rate=8, rate_date=date(2026, 1, 1))
        rates = SnapshotRates(self.snapshot, [self.cash, self.holding])
        self.assertIsNone(rates.convert(Decimal("12"), "EUR", "CNY"))

    def test_history_uses_saved_holdings_and_hides_trade_actions(self):
        InvestmentPosition.objects.create(account=self.account, security=self.security, quantity=99, avg_cost=100, position_date=date(2026, 9, 29))
        with patch("portfolio.views._account_dashboard_data", side_effect=AssertionError("live path")):
            response = self.page()
        position = response.context["historical_positions"][0]
        self.assertEqual(position["quantity"], 2)
        self.assertEqual(position["realized"], 8)
        self.assertNotContains(response, "更新价格")
        self.assertNotContains(response, "action=sell")
        self.assertContains(response, "未统计")
        self.assertContains(response, "不是该年收益")

    def test_account_link_preserves_year_and_inactive_account_is_visible(self):
        bank = self.account.bank_account
        bank.is_active = False
        bank.supports_investment = False
        bank.save()
        response = self.page()
        self.assertContains(response, f'/portfolio/accounts/{self.account.pk}/?currency=CNY&year=2025')
        detail = self.client.get(reverse("portfolio:account_detail", args=[self.account.pk]), {"year": "2025"})
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.context["account_rows"][0]["total_asset"], 840)
        self.assertContains(detail, "返回同年账户汇总")

    def test_unknown_year_has_no_current_balance_fallback(self):
        response = self.page(year="2023")
        self.assertEqual(response.context["account_rows"], [])
        self.assertContains(response, "所选年份没有可用快照")

    def test_missing_account_lines_are_unknown_not_zero(self):
        missing = create_broker_investment_account(self.family, self.member, "缺少历史余额")
        InvestmentTransaction.objects.create(account=missing, trade_date=date(2025, 1, 1), currency="CNY", trade_type="dividend", realized_pnl=1)
        response = self.page()
        row = next(row for row in response.context["account_rows"] if row["account"].pk == missing.pk)
        self.assertIsNone(row["total_asset"])
        self.assertEqual(row["realized"], 1)
        self.assertContains(response, "缺少快照明细")
        self.assertIsNone(response.context["summary_rows"][0]["total_asset"])

    def test_future_accounts_are_not_added_as_historical_zeroes(self):
        create_broker_investment_account(self.family, self.member, "未来开户", currency="CNY", cash_balance=100)
        self.assertNotContains(self.page(), "未来开户")

    def test_partial_snapshot_is_explicit_and_partial_sum_is_not_total(self):
        self.snapshot.extra_data = {"complete": False, "missing_prices": [{"account_id": self.account.pk, "security": "US:MISSING"}]}
        self.snapshot.save()
        response = self.page()
        self.assertIsNone(response.context["account_rows"][0]["total_asset"])
        self.assertContains(response, "数据不完整")
        self.assertContains(response, "缺价 1")

    def test_other_household_cannot_access_historical_account(self):
        other = Family.objects.create(name="其他家庭")
        member = FamilyMember.objects.create(family=other, display_name="其他成员")
        account = create_broker_investment_account(other, member, "其他证券账户")
        response = self.client.get(reverse("portfolio:account_detail", args=[account.pk]), {"year": "2025"})
        self.assertEqual(response.status_code, 404)

    def test_each_year_selects_its_own_final_snapshot(self):
        earlier = PortfolioSnapshot.objects.create(family=self.family, snapshot_date=date(2024, 12, 31), currency="CNY", extra_data={"complete": True})
        PortfolioSnapshotPositionLine.objects.create(snapshot=earlier, account=self.account, asset_type="cash", asset_name="现金", currency="USD", market_value=70, market_value_original=10, fx_rate=7)
        self.assertEqual(self.page(year="2024").context["account_rows"][0]["realized"], 21)
        later = PortfolioSnapshot.objects.create(family=self.family, snapshot_date=date(2026, 9, 29), currency="CNY", extra_data={"complete": True})
        PortfolioSnapshotPositionLine.objects.create(snapshot=later, account=self.account, asset_type="cash", asset_name="现金", currency="USD", market_value=140, market_value_original=20, fx_rate=7)
        response = self.page(year="2026")
        self.assertEqual(response.context["account_rows"][0]["realized"], 686)
        self.assertEqual(response.context["balance_snapshot_date"], date(2026, 9, 29))
