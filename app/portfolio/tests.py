import json
from io import StringIO
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from unittest.mock import MagicMock, patch

from family_core.models import (
    AccountRegion,
    AccountType,
    Currency,
    ExchangeRate,
    Family,
    FamilyMember,
    SiteSetting,
)
from ipo.models import HkIpoListing, HkIpoSubscriptionTrade
from ledger.models import BankAccount

from .exchange_rate_service import ensure_daily_exchange_rates
from .account_sync import sync_investment_account
from .ipo_sync import sync_ipo_trade
from .models import (
    DailyExchangeRateFetch,
    InvestmentAccount,
    InvestmentCashMovement,
    InvestmentOption,
    InvestmentPosition,
    InvestmentTransaction,
    OptionContract,
    PortfolioSnapshot,
    PortfolioSnapshotPositionLine,
    Security,
    SecurityMarketSnapshot,
    TradeTypeChoices,
    WatchlistItem,
)
from .services import rebuild_position


def create_broker_investment_account(
    family,
    member,
    account_name,
    currency=None,
    cash_balance=None,
):
    account_type, _ = AccountType.objects.get_or_create(
        family=family,
        name="券商",
    )
    bank_account = BankAccount.objects.create(
        family=family,
        member=member,
        account_name=account_name,
        account_type_ref=account_type,
        supports_investment=True,
    )
    account = sync_investment_account(bank_account)
    if currency and cash_balance is not None:
        InvestmentCashMovement.objects.create(
            account=account,
            movement_date=date(2026, 1, 1),
            movement_type="deposit",
            currency=currency,
            amount=cash_balance,
        )
    return account


class DailyExchangeRateTests(TestCase):
    @patch("portfolio.exchange_rate_service.urlopen")
    def test_fetches_chinamoney_once_per_day_and_caches_both_rates(self, urlopen):
        response = MagicMock()
        response.read.return_value = json.dumps(
            {
                "data": {"lastDate": "2026-07-03 9:15"},
                "records": [
                    {"vrtEName": "USD/CNY", "price": "6.8047"},
                    {"vrtEName": "HKD/CNY", "price": "0.86754"},
                ],
            }
        ).encode()
        urlopen.return_value.__enter__.return_value = response

        first = ensure_daily_exchange_rates()
        second = ensure_daily_exchange_rates()

        self.assertEqual(urlopen.call_count, 1)
        self.assertEqual(first["usd_cny"], Decimal("6.80470000"))
        self.assertEqual(second["hkd_cny"], Decimal("0.86754000"))
        self.assertEqual(DailyExchangeRateFetch.objects.count(), 1)
        self.assertEqual(
            ExchangeRate.objects.filter(
                quote_currency="CNY",
                rate_date=date(2026, 7, 3),
            ).count(),
            2,
        )


class IpoPortfolioSyncTests(TestCase):
    def test_allotment_and_sale_create_idempotent_portfolio_transactions(self):
        family = Family.objects.create(name="IPO 同步家庭")
        member = FamilyMember.objects.create(family=family, display_name="成员")
        account_type = AccountType.objects.create(family=family, name="券商")
        source_account = BankAccount.objects.create(
            family=family,
            member=member,
            account_name="港股打新账户",
            account_type_ref=account_type,
            supports_ipo=True,
        )
        listing = HkIpoListing.objects.create(
            stock_code="02500.HK",
            stock_name="测试新股",
            company_name="测试新股有限公司",
            subscription_end_date=date(2026, 7, 1),
            allotment_result_date=date(2026, 7, 3),
            final_price=Decimal("20"),
            lot_size=100,
        )

        ipo_trade = HkIpoSubscriptionTrade.objects.create(
            listing=listing,
            member=member,
            account=source_account,
            application_date=date(2026, 7, 1),
            applied_lots=2,
            allotted_lots=2,
            subscription_fee=Decimal("100"),
            financing_interest=Decimal("4"),
            sold_lots=1,
            sell_price=Decimal("25"),
            sell_date=date(2026, 7, 4),
            trading_fee=Decimal("15"),
            remark="来自打新申购表",
        )
        sync_ipo_trade(ipo_trade.pk)

        transactions = InvestmentTransaction.objects.filter(
            external_id__startswith=f"ipo:{ipo_trade.pk}:"
        ).order_by("trade_date")
        self.assertEqual(transactions.count(), 1)
        buy = transactions.get()
        self.assertEqual(buy.trade_type, TradeTypeChoices.IPO)
        self.assertEqual(buy.quantity, Decimal("200"))
        self.assertEqual(buy.currency, "HKD")
        self.assertEqual(buy.trade_date, date(2026, 7, 3))
        self.assertTrue(buy.transaction_no)

        ipo_trade.remark = "更新后的备注"
        ipo_trade.save()
        sync_ipo_trade(ipo_trade.pk)
        self.assertEqual(
            InvestmentTransaction.objects.filter(
                external_id__startswith=f"ipo:{ipo_trade.pk}:"
            ).count(),
            1,
        )
        self.assertFalse(
            InvestmentTransaction.objects.filter(
                external_id__startswith=f"ipo:{ipo_trade.pk}:"
            ).exclude(remark="更新后的备注")
        )


class TransactionFormTests(TestCase):
    def test_form_uses_family_member_security_account_and_database_options(self):
        user = get_user_model().objects.create_user(
            username="transaction-form-tester",
            password="test-password",
        )
        family = Family.objects.create(name="交易录入家庭")
        member = FamilyMember.objects.create(
            family=family,
            user=user,
            display_name="录入用户",
        )
        account_type = AccountType.objects.create(family=family, name="券商")
        bank_account = BankAccount.objects.create(
            family=family,
            member=member,
            account_type_ref=account_type,
            account_name="测试券商账户",
            supports_investment=True,
        )
        security = Security.objects.create(
            symbol="00700",
            name="腾讯控股",
            market="HK",
            currency="HKD",
        )
        WatchlistItem.objects.create(
            family=family,
            member=member,
            security=security,
        )
        self.client.force_login(user)

        form_page = self.client.get(reverse("portfolio:transaction_create"))
        options = self.client.get(
            reverse("portfolio:transaction_form_options"),
            {"family": family.pk, "member": member.pk},
        )

        self.assertContains(form_page, "家庭")
        self.assertContains(form_page, "用户")
        self.assertContains(form_page, "证券账户")
        self.assertContains(form_page, "测试券商账户")
        self.assertContains(form_page, "自动根据交易标的")
        self.assertNotContains(form_page, "外部流水号")
        self.assertNotContains(form_page, "交易状态")
        self.assertEqual(options.status_code, 200)
        self.assertEqual(options.json()["accounts"][0]["name"], "测试券商账户")

        buy_option = InvestmentOption.objects.get(
            category=InvestmentOption.CATEGORY_TRANSACTION_TYPE,
            code=TradeTypeChoices.BUY,
        )
        response = self.client.post(
            reverse("portfolio:transaction_create"),
            {
                "family": family.pk,
                "member": member.pk,
                "bank_account": bank_account.pk,
                "security": security.pk,
                "trade_date": "2026-07-05",
                "trade_type_option": buy_option.pk,
                "currency": "",
                "quantity": "10",
                "price": "500",
                "amount": "5000",
                "fee": "5",
                "tax": "0",
                "trade_logic": "",
                "information_source_option": "",
                "strategy_option": "",
                "strategy_other": "",
                "emotion_option": "",
                "exit_condition": "",
                "remark": "",
            },
        )
        self.assertEqual(response.status_code, 302)
        transaction = InvestmentTransaction.objects.get()
        self.assertEqual(transaction.account.bank_account, bank_account)
        self.assertEqual(transaction.currency, "HKD")

    def test_option_contract_form_keeps_option_distinct_from_underlying(self):
        user = get_user_model().objects.create_user(username="option-form-tester")
        family = Family.objects.create(name="期权家庭")
        member = FamilyMember.objects.create(family=family, user=user, display_name="成员")
        Currency.objects.update_or_create(
            code="USD", defaults={"name": "美元", "symbol": "$", "is_active": True}
        )
        underlying = Security.objects.create(
            symbol="MSFT",
            name="微软",
            market="US",
            asset_type=Security.TYPE_STOCK,
            currency="USD",
        )
        self.client.force_login(user)

        response = self.client.post(
            reverse("portfolio:option_contract_create"),
            {
                "underlying": underlying.pk,
                "contract_symbol": "MSFT260717P00300000",
                "option_type": OptionContract.PUT,
                "strike_price": "300",
                "expiration_date": "2026-07-17",
                "multiplier": "100",
                "market": "US",
                "currency": "USD",
                "asset_category": "",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(Security.objects.filter(symbol="MSFT", market="US").count(), 1)
        contract = OptionContract.objects.get()
        self.assertEqual(contract.security.symbol, "MSFT260717P00300000")
        self.assertEqual(contract.underlying, underlying)
        self.assertTrue(
            WatchlistItem.objects.filter(family=family, security=contract.security).exists()
        )


class AccountPrototypeTests(TestCase):
    def test_prototype_and_detail_pages_are_reviewable(self):
        user = get_user_model().objects.create_user(
            username="prototype-tester",
            password="test-password",
        )
        family = Family.objects.create(name="原型测试家庭")
        FamilyMember.objects.create(
            family=family,
            user=user,
            display_name="原型测试成员",
        )
        self.client.force_login(user)

        overview = self.client.get(reverse("portfolio:account_prototype"))
        detail = self.client.get(
            reverse("portfolio:account_detail_prototype", args=[1]),
            {"tab": "positions"},
        )
        transactions = self.client.get(
            reverse("portfolio:account_detail_prototype", args=[1]),
            {"tab": "transactions"},
        )

        self.assertContains(overview, "家庭汇总")
        self.assertContains(overview, "统计年份")
        self.assertContains(overview, "持仓占比")
        self.assertContains(overview, "累计已实现盈亏")
        self.assertContains(overview, "持仓明细")
        self.assertContains(overview, "查看账户")
        self.assertContains(detail, "持仓资产")
        self.assertContains(detail, "现金流水")
        self.assertContains(transactions, "本次卖出收益率")
        self.assertContains(transactions, "交易逻辑")


class InvestmentCashMovementFormTests(TestCase):
    def test_broker_account_sync_and_deposit_form_use_bank_accounts(self):
        user = get_user_model().objects.create_user(
            username="cash-movement-tester",
            password="test-password",
        )
        family = Family.objects.create(name="现金流水家庭")
        member = FamilyMember.objects.create(
            family=family,
            user=user,
            display_name="现金用户",
        )
        broker_type = AccountType.objects.create(family=family, name="券商")
        bank_type = AccountType.objects.create(family=family, name="银行")
        domestic = AccountRegion.objects.create(family=family, name="境内")
        overseas = AccountRegion.objects.create(family=family, name="境外")
        Currency.objects.create(code="HKD", name="港币", symbol="HK$")
        broker = BankAccount.objects.create(
            family=family,
            member=member,
            account_name="富途证券",
            account_no_masked="***8888",
            account_type_ref=broker_type,
            account_region=overseas,
            supports_investment=True,
        )
        foreign_bank = BankAccount.objects.create(
            family=family,
            member=member,
            account_name="中银香港",
            account_type_ref=bank_type,
            account_region=overseas,
        )
        BankAccount.objects.create(
            family=family,
            member=member,
            account_name="招商银行",
            account_type_ref=bank_type,
            account_region=domestic,
        )
        account = sync_investment_account(broker)
        self.assertEqual(account.account_name, "富途证券")
        self.assertEqual(account.account_no_masked, "***8888")
        self.assertEqual(account.account_region, overseas)

        self.client.force_login(user)
        page = self.client.get(
            reverse("portfolio:cash_movement_create", args=[account.pk])
        )
        self.assertContains(page, "富途证券")
        self.assertContains(page, "中银香港")
        self.assertNotContains(page, "招商银行")
        movement_choices = {
            value for value, _label in page.context["form"].fields["movement_type"].choices
        }
        self.assertIn("exchange", movement_choices)
        self.assertIn("transfer", movement_choices)
        self.assertNotIn("buy", movement_choices)

        response = self.client.post(
            reverse("portfolio:cash_movement_create", args=[account.pk]),
            {
                "movement_type": "deposit",
                "amount": "10000",
                "currency": "HKD",
                "movement_date": "2026-07-05",
                "counterparty_account": foreign_bank.pk,
                "remark": "首次入金",
            },
        )
        self.assertEqual(response.status_code, 302)
        movement = InvestmentCashMovement.objects.get(account=account)
        self.assertEqual(movement.amount, Decimal("10000"))
        self.assertEqual(movement.counterparty_account, foreign_bank)


class AccountDashboardTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="account-dashboard-tester",
            password="test-password",
        )
        family = Family.objects.create(name="账户页面家庭", base_currency="CNY")
        member = FamilyMember.objects.create(
            family=family,
            user=self.user,
            display_name="成员甲",
        )
        self.account = create_broker_investment_account(
            family,
            member,
            "证券账户",
            currency="CNY",
            cash_balance=Decimal("10000"),
        )
        self.client.force_login(self.user)

    def test_dashboard_and_account_tabs_use_real_account(self):
        dashboard = self.client.get(reverse("portfolio:account_list"))
        detail = self.client.get(
            reverse("portfolio:account_detail", args=[self.account.pk]),
            {"tab": "cashflows"},
        )

        self.assertContains(dashboard, "证券账户")
        self.assertContains(dashboard, "持仓明细")
        self.assertContains(detail, "变动后本位币余额")

    def test_accounts_sort_by_total_asset_descending(self):
        second = create_broker_investment_account(
            self.account.family,
            self.account.member,
            "高资产账户",
            currency="CNY",
            cash_balance=Decimal("20000"),
        )

        response = self.client.get(reverse("portfolio:account_list"))

        self.assertEqual(
            response.context["account_rows"][0]["account"],
            second,
        )

    def test_balance_is_current_and_not_limited_to_selected_year(self):
        InvestmentCashMovement.objects.create(
            account=self.account,
            movement_date=date(2025, 12, 31),
            movement_type="deposit",
            currency="CNY",
            amount=Decimal("500"),
        )

        all_years = self.client.get(reverse("portfolio:account_list"), {"year": "all"})
        current_year = self.client.get(reverse("portfolio:account_list"), {"year": "2026"})

        self.assertEqual(all_years.context["account_rows"][0]["cash"], Decimal("10500"))
        self.assertEqual(current_year.context["account_rows"][0]["cash"], Decimal("10500"))

    def test_detail_can_switch_to_diluted_cost_and_has_no_duplicate_actions(self):
        security = Security.objects.create(
            symbol="TEST",
            name="成本测试",
            market="CN",
            currency="CNY",
        )
        InvestmentPosition.objects.create(
            account=self.account,
            security=security,
            quantity=Decimal("10"),
            avg_cost=Decimal("100"),
            diluted_cost=Decimal("80"),
            current_price=Decimal("120"),
            market_value=Decimal("1200"),
            position_date=date(2026, 7, 5),
        )

        positions = self.client.get(
            reverse("portfolio:account_detail", args=[self.account.pk]),
            {"tab": "positions", "cost_method": "diluted"},
        )
        cashflows = self.client.get(
            reverse("portfolio:account_detail", args=[self.account.pk]),
            {"tab": "cashflows"},
        )
        transactions = self.client.get(
            reverse("portfolio:account_detail", args=[self.account.pk]),
            {"tab": "transactions"},
        )

        position = positions.context["positions"][0]
        self.assertEqual(position.display_cost, Decimal("80"))
        self.assertEqual(position.unrealized_live, Decimal("400"))
        self.assertContains(positions, "摊薄成本")
        self.assertNotContains(cashflows, "新增现金变动")
        self.assertEqual(
            transactions.content.decode().count("新增交易"),
            1,
        )

    def test_manual_transaction_can_be_deleted_and_position_is_rebuilt(self):
        security = Security.objects.create(
            symbol="DELETE",
            name="删除测试",
            market="HK",
            currency="HKD",
        )
        item = InvestmentTransaction.objects.create(
            account=self.account,
            security=security,
            trade_date=date(2026, 7, 5),
            trade_type=TradeTypeChoices.BUY,
            quantity=Decimal("1"),
            price=Decimal("10"),
            amount=Decimal("10"),
            fee=Decimal("0.25"),
            currency="HKD",
        )
        rebuild_position(self.account, security)

        page = self.client.get(
            reverse("portfolio:account_detail", args=[self.account.pk]),
            {"tab": "transactions"},
        )
        self.assertEqual(page.context["transactions"][0].total_fee, Decimal("0.25"))
        self.assertContains(page, reverse("portfolio:transaction_edit", args=[item.pk]))
        response = self.client.post(reverse("portfolio:transaction_delete", args=[item.pk]))

        self.assertEqual(response.status_code, 302)
        self.assertFalse(InvestmentTransaction.objects.filter(pk=item.pk).exists())
        self.assertEqual(
            InvestmentPosition.objects.get(account=self.account, security=security).quantity,
            Decimal("0"),
        )


class HistoricalIpoBackfillTests(TestCase):
    def test_command_is_dry_run_by_default_and_idempotent_on_apply(self):
        family = Family.objects.create(name="历史回填家庭")
        member = FamilyMember.objects.create(family=family, display_name="成员")
        account = create_broker_investment_account(family, member, "历史券商").bank_account
        listing = HkIpoListing.objects.create(
            stock_code="09988.HK",
            stock_name="历史新股",
            subscription_end_date=date(2025, 1, 2),
            allotment_result_date=date(2025, 1, 4),
            final_price=Decimal("10"),
            lot_size=100,
        )
        ipo_trade = HkIpoSubscriptionTrade.objects.create(
            listing=listing,
            member=member,
            account=account,
            allotted_lots=1,
            sold_lots=1,
            sell_date=date(2025, 1, 5),
            sell_price=Decimal("12"),
            subscription_fee=Decimal("100"),
            trading_fee=Decimal("10"),
        )
        self.assertEqual(ipo_trade.realized_profit, Decimal("80"))

        output = StringIO()
        call_command("backfill_ipo_transactions", stdout=output)
        self.assertIn("DRY-RUN", output.getvalue())
        self.assertFalse(ipo_trade.investment_transactions.exists())

        call_command("backfill_ipo_transactions", apply=True, stdout=StringIO())
        call_command("backfill_ipo_transactions", apply=True, stdout=StringIO())

        self.assertEqual(ipo_trade.investment_transactions.count(), 2)
        self.assertEqual(
            InvestmentTransaction.objects.get(
                ipo_subscription_trade=ipo_trade,
                trade_type=TradeTypeChoices.SELL,
            ).realized_pnl,
            Decimal("80"),
        )


class PortfolioOverviewTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="portfolio-tester",
            password="test-password",
        )
        family = Family.objects.create(name="测试家庭")
        self.member = FamilyMember.objects.create(
            family=family,
            user=self.user,
            display_name="成员甲",
        )
        ExchangeRate.objects.create(
            base_currency="HKD",
            quote_currency="CNY",
            rate=Decimal("0.9"),
            rate_date=date(2026, 7, 5),
        )
        ExchangeRate.objects.create(
            base_currency="USD",
            quote_currency="CNY",
            rate=Decimal("7"),
            rate_date=date(2026, 7, 5),
        )
        self.account = create_broker_investment_account(
            family,
            self.member,
            "港股账户",
            currency="HKD",
            cash_balance=Decimal("1000"),
        )
        self.security = Security.objects.create(
            symbol="00700.HK",
            name="腾讯控股",
            market="HK",
            currency="HKD",
        )
        self.latest_position = InvestmentPosition.objects.create(
            account=self.account,
            security=self.security,
            quantity=Decimal("20"),
            avg_cost=Decimal("300"),
            current_price=Decimal("350"),
            market_value=Decimal("7000"),
            unrealized_pnl=Decimal("1000"),
            pnl_ratio=Decimal("0.166667"),
            position_date=date(2026, 7, 3),
        )
        self.client.force_login(self.user)

    def test_overview_uses_unique_current_position_for_each_account_and_security(self):
        response = self.client.get(reverse("portfolio:overview"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["total_cash"], Decimal("900"))
        self.assertEqual(response.context["total_market_value"], Decimal("6300"))
        self.assertEqual(response.context["total_cost"], Decimal("5400"))
        self.assertEqual(response.context["total_pnl"], Decimal("900"))
        self.assertEqual(response.context["total_asset"], Decimal("7200"))
        self.assertEqual(response.context["base_currency"], "CNY")
        self.assertEqual(PortfolioSnapshot.objects.count(), 0)

    def test_account_page_converts_hkd_to_usd_through_cny_rates(self):
        response = self.client.get(reverse("portfolio:account_list"), {"currency": "USD"})

        self.assertEqual(response.status_code, 200)
        hkd_cny = ExchangeRate.objects.filter(base_currency="HKD", quote_currency="CNY").order_by("-rate_date").first()
        usd_cny = ExchangeRate.objects.filter(base_currency="USD", quote_currency="CNY").order_by("-rate_date").first()
        summary = response.context["summary_rows"][0]
        self.assertEqual(summary["cash"], Decimal("1000") * hkd_cny.rate / usd_cny.rate)
        self.assertEqual(
            summary["market_value"].quantize(Decimal("0.0001")),
            (Decimal("7000") * hkd_cny.rate / usd_cny.rate).quantize(Decimal("0.0001")),
        )
        self.assertFalse(response.context["missing_exchange_rates"])

    def test_overview_member_selector_updates_all_content(self):
        second = FamilyMember.objects.create(
            family=self.member.family,
            display_name="成员乙",
        )
        create_broker_investment_account(
            self.member.family,
            second,
            "成员乙账户",
            currency="CNY",
            cash_balance=Decimal("500"),
        )

        response = self.client.get(
            reverse("portfolio:overview"),
            {"member": second.pk},
        )

        self.assertEqual(response.context["selected_member"], second)
        self.assertEqual(response.context["total_asset"], Decimal("500"))
        self.assertContains(response, "成员乙")

    def test_overview_uses_existing_snapshots_for_trend(self):
        PortfolioSnapshot.objects.create(
            family=self.member.family,
            member=self.member,
            snapshot_date=date(2026, 7, 4),
            total_asset=Decimal("5000"),
            currency="CNY",
        )

        response = self.client.get(reverse("portfolio:overview"))

        self.assertEqual(len(response.context["trend_snapshots"]), 1)
        self.assertEqual(response.context["change_amount"], Decimal("0"))
        self.assertTrue(response.context["trend_points"])


class PortfolioSnapshotCommandTests(TestCase):
    def test_command_uses_cached_quote_and_is_idempotent(self):
        family = Family.objects.create(name="Snapshot Family")
        member = FamilyMember.objects.create(family=family, display_name="Member")
        SiteSetting.objects.update_or_create(
            pk=1,
            defaults={"household_name": "Snapshot Family", "base_currency": "CNY"},
        )
        ExchangeRate.objects.create(
            base_currency="HKD",
            quote_currency="CNY",
            rate=Decimal("0.9"),
            rate_date=date.today(),
        )
        account = create_broker_investment_account(
            family,
            member,
            "Snapshot Account",
            currency="HKD",
            cash_balance=Decimal("1000"),
        )
        security = Security.objects.create(
            symbol="SNAP",
            name="Snapshot ETF",
            market="HK",
            asset_type="etf",
            currency="HKD",
        )
        SecurityMarketSnapshot.objects.create(
            security=security,
            last_price=Decimal("400"),
        )
        position = InvestmentPosition.objects.create(
            account=account,
            security=security,
            quantity=Decimal("10"),
            avg_cost=Decimal("300"),
            current_price=Decimal("350"),
            position_date=date.today(),
        )

        call_command("create_portfolio_snapshots")
        call_command("create_portfolio_snapshots")

        self.assertEqual(PortfolioSnapshot.objects.count(), 2)
        family_snapshot = PortfolioSnapshot.objects.get(member=None)
        self.assertEqual(family_snapshot.total_asset, Decimal("4500"))
        self.assertEqual(family_snapshot.position_lines.count(), 2)
        self.assertTrue(
            PortfolioSnapshotPositionLine.objects.filter(
                snapshot=family_snapshot,
                asset_type="etf",
                price=Decimal("400"),
            ).exists()
        )
        position.refresh_from_db()
        self.assertEqual(position.current_price, Decimal("400"))


class PositionAccountingTests(TestCase):
    def setUp(self):
        family = Family.objects.create(name="核算测试家庭")
        member = FamilyMember.objects.create(family=family, display_name="成员甲")
        self.account = create_broker_investment_account(
            family,
            member,
            "测试账户",
        )
        self.security = Security.objects.create(
            symbol="00001.HK",
            name="核算测试股",
            market="HK",
            currency="HKD",
        )

    def add_trade(self, trade_date, trade_type, quantity, price, fee="0"):
        return InvestmentTransaction.objects.create(
            account=self.account,
            security=self.security,
            trade_date=trade_date,
            trade_type=trade_type,
            quantity=Decimal(quantity),
            price=Decimal(price),
            amount=Decimal(quantity) * Decimal(price),
            fee=Decimal(fee),
            currency="HKD",
        )

    def test_rebuild_calculates_moving_average_diluted_cost_and_realized_pnl(self):
        self.add_trade(date(2026, 1, 1), TradeTypeChoices.BUY, "10", "100", "10")
        self.add_trade(date(2026, 1, 2), TradeTypeChoices.BUY, "10", "120", "10")
        sale = self.add_trade(
            date(2026, 1, 3), TradeTypeChoices.SELL, "5", "150", "5"
        )

        position = rebuild_position(self.account, self.security)
        position.refresh_from_db()
        sale.refresh_from_db()

        self.assertEqual(position.quantity, Decimal("15"))
        self.assertEqual(position.avg_cost, Decimal("111"))
        self.assertEqual(position.diluted_cost, Decimal("98.333333"))
        self.assertEqual(position.realized_pnl, Decimal("190"))
        self.assertEqual(position.unrealized_pnl, Decimal("585"))
        self.assertEqual(sale.cash_change, Decimal("745"))
        self.assertEqual(sale.sell_cost, Decimal("555"))
        self.assertEqual(sale.realized_pnl, Decimal("190"))
        self.assertEqual(
            InvestmentCashMovement.objects.filter(account=self.account).count(),
            3,
        )

    def test_reopened_position_keeps_historical_profit_in_diluted_cost(self):
        self.add_trade(date(2026, 1, 1), TradeTypeChoices.BUY, "10", "100")
        self.add_trade(date(2026, 1, 2), TradeTypeChoices.SELL, "10", "120")
        self.add_trade(date(2026, 1, 3), TradeTypeChoices.BUY, "5", "110")

        position = rebuild_position(self.account, self.security)
        position.refresh_from_db()

        self.assertEqual(position.quantity, Decimal("5"))
        self.assertEqual(position.avg_cost, Decimal("110"))
        self.assertEqual(position.diluted_cost, Decimal("70"))
        self.assertEqual(position.realized_pnl, Decimal("200"))

    def test_rebuild_rejects_selling_more_than_the_holding(self):
        self.add_trade(date(2026, 1, 1), TradeTypeChoices.BUY, "10", "100")
        self.add_trade(date(2026, 1, 2), TradeTypeChoices.SELL, "11", "120")

        with self.assertRaisesMessage(ValidationError, "超过当时持仓"):
            rebuild_position(self.account, self.security)

    def test_option_short_open_and_buy_close(self):
        underlying = Security.objects.create(
            symbol="MSFT",
            name="微软",
            market="US",
            currency="USD",
        )
        option = Security.objects.create(
            symbol="MSFT260717P00300000",
            name="微软看跌期权",
            market="US",
            asset_type=Security.TYPE_OPTION,
            currency="USD",
        )
        OptionContract.objects.create(
            security=option,
            underlying=underlying,
            option_type=OptionContract.PUT,
            strike_price=Decimal("300"),
            expiration_date=date(2026, 7, 17),
            multiplier=100,
        )
        open_short = InvestmentTransaction.objects.create(
            account=self.account,
            security=option,
            trade_date=date(2026, 1, 1),
            trade_type=TradeTypeChoices.SELL,
            position_effect=InvestmentTransaction.EFFECT_OPEN,
            quantity=Decimal("1"),
            price=Decimal("5"),
            amount=Decimal("500"),
            fee=Decimal("1"),
            currency="USD",
        )
        close_short = InvestmentTransaction.objects.create(
            account=self.account,
            security=option,
            trade_date=date(2026, 1, 2),
            trade_type=TradeTypeChoices.BUY,
            position_effect=InvestmentTransaction.EFFECT_CLOSE,
            quantity=Decimal("1"),
            price=Decimal("2"),
            amount=Decimal("200"),
            fee=Decimal("1"),
            currency="USD",
        )

        position = rebuild_position(self.account, option)
        open_short.refresh_from_db()
        close_short.refresh_from_db()

        self.assertEqual(position.quantity, Decimal("0"))
        self.assertEqual(position.realized_pnl, Decimal("298"))
        self.assertEqual(open_short.cash_change, Decimal("499"))
        self.assertEqual(close_short.cash_change, Decimal("-201"))
        self.assertEqual(close_short.realized_pnl, Decimal("298"))

    def test_transaction_numbers_are_unique_without_max_plus_one(self):
        first = self.add_trade(date(2026, 1, 1), TradeTypeChoices.BUY, "1", "100")
        second = self.add_trade(date(2026, 1, 1), TradeTypeChoices.BUY, "1", "100")

        self.assertTrue(first.transaction_no.startswith("TXN-"))
        self.assertNotEqual(first.transaction_no, second.transaction_no)

    def test_dividend_and_tax_create_cash_and_realized_income(self):
        self.add_trade(date(2026, 1, 1), TradeTypeChoices.BUY, "10", "100")
        dividend = InvestmentTransaction.objects.create(
            account=self.account,
            security=self.security,
            trade_date=date(2026, 1, 2),
            trade_type=TradeTypeChoices.DIVIDEND,
            amount=Decimal("100"),
            fee=Decimal("2"),
            tax=Decimal("5"),
            currency="HKD",
        )

        position = rebuild_position(self.account, self.security)
        dividend.refresh_from_db()

        self.assertEqual(dividend.cash_change, Decimal("93"))
        self.assertEqual(dividend.realized_pnl, Decimal("93"))
        self.assertEqual(position.realized_pnl, Decimal("93"))
        self.assertEqual(dividend.cash_movement.amount, Decimal("93"))


class WatchlistPageTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="watchlist-tester",
            password="test-password",
        )
        self.family = Family.objects.create(name="自选股测试家庭")
        self.member = FamilyMember.objects.create(
            family=self.family,
            user=self.user,
            display_name="成员甲",
        )
        self.client.force_login(self.user)
        self.futu_result = {
            "code": "HK.00700",
            "symbol": "00700",
            "market": "HK",
            "exchange": "HK",
            "name": "腾讯控股",
            "asset_type": "stock",
            "currency": "HKD",
            "lot_size": 100,
            "listing_date": "2004-06-16",
            "is_delisted": False,
            "last_price": 500.2,
            "change_rate": 2.35,
            "quote_time": "2026-07-04 16:08:00",
            "total_market_value": 4560000000000,
            "pe_ratio": 18.1,
            "pe_ttm_ratio": 17.5,
            "pb_ratio": 4.2,
            "ps_ratio": 6.3,
            "dividend_yield_ttm": 1.1,
            "turnover_rate": 0.4,
            "high_52_week": 550,
            "low_52_week": 300,
            "issued_shares": 9100000000,
            "outstanding_shares": 9000000000,
            "futu_url": "https://www.futunn.com/hk/stock/00700-HK",
            "raw_data": {"stock_id": 10000700},
        }

    @patch("portfolio.views.search_futu_securities")
    def test_query_shows_futu_result_without_saving_it(self, search):
        search.return_value = [self.futu_result]

        response = self.client.get(
            reverse("portfolio:security_list"),
            {"market": "HK", "q": "腾讯"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "腾讯控股")
        self.assertContains(response, "07-04")
        self.assertContains(response, "2.35%")
        self.assertContains(response, "4.56 万亿")
        self.assertContains(response, 'class="positive">500.20')
        self.assertContains(response, "市盈率（静）")
        self.assertContains(response, "市盈率（TTM）")
        self.assertContains(response, "市净率")
        self.assertContains(response, "市销率")
        self.assertNotContains(response, "<th>市场</th>", html=True)
        self.assertNotContains(response, "<th>币种</th>", html=True)
        self.assertNotContains(response, "<th>上市日期</th>", html=True)
        self.assertNotContains(response, "<th>每手</th>", html=True)
        self.assertContains(response, "添加到自选股")
        self.assertFalse(WatchlistItem.objects.exists())

    @patch("portfolio.views.search_futu_securities")
    def test_add_result_saves_security_snapshot_and_watchlist_item(self, search):
        search.return_value = [self.futu_result]
        response = self.client.get(
            reverse("portfolio:security_list"),
            {"market": "HK", "q": "00700"},
        )
        token = response.context["search_results"][0]["add_token"]

        response = self.client.post(
            reverse("portfolio:watchlist_add"),
            {"token": token},
        )

        self.assertRedirects(response, reverse("portfolio:security_list"))
        security = Security.objects.get(symbol="00700", market="HK")
        snapshot = SecurityMarketSnapshot.objects.get(security=security)
        self.assertTrue(
            WatchlistItem.objects.filter(
                family=self.family,
                security=security,
            ).exists()
        )
        self.assertEqual(security.name, "腾讯控股")
        self.assertEqual(security.lot_size, 100)
        self.assertEqual(snapshot.last_price, Decimal("500.2"))
        self.assertEqual(snapshot.change_rate, Decimal("2.35"))
        self.assertEqual(snapshot.ps_ratio, Decimal("6.3"))
        self.assertEqual(
            security.futu_url,
            "https://www.futunn.com/hk/stock/00700-HK",
        )
