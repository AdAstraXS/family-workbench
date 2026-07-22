import json
from io import StringIO
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone
from unittest.mock import MagicMock, patch

from family_core.models import (
    AccountRegion,
    AccountType,
    AssetCategory,
    Currency,
    ExchangeRate,
    Family,
    FamilyMember,
    SiteSetting,
)
from ipo.models import HkIpoListing, HkIpoSubscriptionTrade
from ledger.models import AssetBalanceEntry, AssetBalanceSnapshot, BankAccount

from .exchange_rate_service import ensure_daily_exchange_rates
from .futu_service import FutuQueryError
from .market_data import record_security_price, refresh_market_data
from .account_sync import sync_investment_account
from .ipo_sync import sync_ipo_trade
from .models import (
    BondDetail,
    CashMovementTypeChoices,
    DailyExchangeRateFetch,
    InvestmentAccount,
    InvestmentCashMovement,
    InvestmentOption,
    InvestmentPosition,
    InvestmentTransaction,
    MarketDataRunStatusChoices,
    OptionContract,
    PortfolioReconciliationRun,
    PortfolioSnapshot,
    PortfolioSnapshotPositionLine,
    PriceSourceChoices,
    PricingStatusChoices,
    Security,
    SecurityExchange,
    SecurityMarket,
    SecurityMarketSnapshot,
    SecurityPriceRecord,
    SecurityQuoteConfig,
    TransactionSourceChoices,
    TradeTypeChoices,
    WatchlistItem,
)
from .services import rebuild_position
from .reconciliation import (
    apply_reconciliation,
    build_reconciliation_preview,
    revert_reconciliation,
)
from .snapshot_service import create_portfolio_snapshots_for_date
from .valuation import refresh_position_valuations


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
    def test_unallotted_backfill_rejects_missing_listing_dates(self):
        family = Family.objects.create(name="缺少日期家庭")
        member = FamilyMember.objects.create(family=family, display_name="成员")
        account_type = AccountType.objects.create(family=family, name="券商")
        source_account = BankAccount.objects.create(
            family=family,
            member=member,
            account_name="缺少日期账户",
            account_type_ref=account_type,
            supports_ipo=True,
        )
        listing = HkIpoListing.objects.create(
            stock_code="02601.HK",
            stock_name="缺少日期测试",
            final_price=Decimal("20"),
            lot_size=100,
        )
        HkIpoSubscriptionTrade.objects.create(
            listing=listing,
            member=member,
            account=source_account,
            application_date=date(2026, 7, 1),
            applied_lots=2,
            allotted_lots=0,
            subscription_fee=Decimal("100"),
        )

        with self.assertRaisesMessage(CommandError, "请先到新股资料中补录这两个日期"):
            call_command("backfill_unallotted_ipo_fees", stdout=StringIO())

        self.assertFalse(InvestmentTransaction.objects.exists())

    def test_unallotted_cost_is_synced_and_replaced_when_allotted(self):
        family = Family.objects.create(name="未中签费用家庭")
        member = FamilyMember.objects.create(family=family, display_name="成员")
        account_type = AccountType.objects.create(family=family, name="券商")
        source_account = BankAccount.objects.create(
            family=family,
            member=member,
            account_name="打新账户",
            account_type_ref=account_type,
            supports_ipo=True,
        )
        listing = HkIpoListing.objects.create(
            stock_code="02600.HK",
            stock_name="未中签测试",
            company_name="未中签测试有限公司",
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
            allotted_lots=0,
            subscription_fee=Decimal("100"),
            financing_interest=Decimal("5"),
        )
        HkIpoSubscriptionTrade.objects.filter(pk=ipo_trade.pk).update(
            allotment_fee=Decimal("99")
        )
        ipo_trade.refresh_from_db()

        dry_run = StringIO()
        call_command("backfill_unallotted_ipo_fees", stdout=dry_run)
        self.assertFalse(InvestmentTransaction.objects.exists())
        self.assertIn("待新增=1", dry_run.getvalue())

        call_command("backfill_unallotted_ipo_fees", apply=True, stdout=StringIO())
        adjustment = InvestmentTransaction.objects.get(
            external_id=f"ipo:{ipo_trade.pk}:unallotted-fee"
        )
        self.assertEqual(adjustment.trade_type, TradeTypeChoices.OTHER_FEE_ADJUSTMENT)
        self.assertEqual(adjustment.amount, Decimal("105"))
        self.assertEqual(adjustment.trade_date, date(2026, 7, 3))
        self.assertEqual(adjustment.cash_change, Decimal("-105"))
        self.assertEqual(adjustment.realized_pnl, Decimal("-105"))
        self.assertEqual(adjustment.cash_movement.amount, Decimal("-105"))
        self.assertEqual(adjustment.cash_movement.movement_date, date(2026, 7, 3))
        ipo_trade.refresh_from_db()
        self.assertEqual(ipo_trade.realized_profit, Decimal("-105"))

        listing.allotment_result_date = None
        listing.subscription_end_date = date(2026, 7, 5)
        listing.save(update_fields=["allotment_result_date", "subscription_end_date"])
        sync_ipo_trade(ipo_trade.pk)
        adjustment.refresh_from_db()
        ipo_trade.refresh_from_db()
        self.assertEqual(adjustment.trade_date, date(2026, 7, 7))
        self.assertEqual(adjustment.cash_movement.movement_date, date(2026, 7, 7))
        self.assertEqual(ipo_trade.sell_date, date(2026, 7, 7))

        ipo_trade.allotted_lots = 1
        ipo_trade.save()
        sync_ipo_trade(ipo_trade.pk)
        self.assertFalse(
            InvestmentTransaction.objects.filter(
                external_id=f"ipo:{ipo_trade.pk}:unallotted-fee"
            ).exists()
        )
        self.assertTrue(
            InvestmentTransaction.objects.filter(
                external_id=f"ipo:{ipo_trade.pk}:buy",
                trade_type=TradeTypeChoices.IPO,
            ).exists()
        )

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
        account_form_page = self.client.get(
            reverse("portfolio:transaction_create"),
            {"account": transaction.account_id},
        )
        expected_parent = transaction.account.get_absolute_url() + "?tab=transactions"
        self.assertEqual(account_form_page.context["page_parent_url"], expected_parent)
        self.assertContains(account_form_page, f'href="{expected_parent}"')

        response = self.client.post(
            reverse("portfolio:transaction_create"),
            {
                "family": family.pk,
                "member": member.pk,
                "bank_account": bank_account.pk,
                "security": security.pk,
                "trade_date": "2026-07-06",
                "trade_type_option": buy_option.pk,
                "currency": "",
                "quantity": "1.123456",
                "price": "2.123456",
                "amount": "2.38",
                "fee": "0.10",
                "tax": "0",
                "trade_logic": "",
                "information_source_option": "",
                "strategy_option": "",
                "strategy_other": "",
                "emotion_option": "",
                "exit_condition": "",
                "remark": "精度测试",
            },
        )
        self.assertEqual(response.status_code, 302)
        precise = InvestmentTransaction.objects.get(remark="精度测试")
        self.assertEqual(
            precise.amount,
            (Decimal("1.123456") * Decimal("2.123456")).quantize(Decimal("0.01")),
        )
        edit_page = self.client.get(
            reverse("portfolio:transaction_edit", args=[precise.pk])
        )
        self.assertEqual(edit_page.context["page_parent_url"], expected_parent)
        self.assertIn('value="2.39"', str(edit_page.context["form"]["amount"]))
        self.assertIn('value="2.123456"', str(edit_page.context["form"]["price"]))
        self.assertIn('value="1.123456"', str(edit_page.context["form"]["quantity"]))

    def test_option_contract_form_keeps_option_distinct_from_underlying(self):
        user = get_user_model().objects.create_user(username="option-form-tester")
        family = Family.objects.create(name="期权家庭")
        member = FamilyMember.objects.create(family=family, user=user, display_name="成员")
        Currency.objects.update_or_create(
            code="USD", defaults={"name": "美元", "symbol": "$", "is_active": True}
        )
        derivative_category = AssetCategory.objects.create(
            family=family, name="衍生品", code="derivatives"
        )
        underlying = Security.objects.create(
            symbol="MSFT",
            name="微软",
            market="US",
            asset_type=Security.TYPE_STOCK,
            currency="USD",
        )
        self.client.force_login(user)

        page = self.client.get(reverse("portfolio:option_contract_create"))
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.context["page_parent_url"], reverse("portfolio:security_list"))

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
        self.assertEqual(contract.security.asset_category, derivative_category)
        self.assertNotContains(page, "资产配置类别")
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


class InvestmentCashMovementAdminBoundaryTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser(
            username="cash-admin-tester",
            password="test-password",
            email="cash-admin@example.com",
        )
        family = Family.objects.create(name="现金后台边界家庭")
        member = FamilyMember.objects.create(
            family=family,
            display_name="现金后台管理员",
        )
        self.account = create_broker_investment_account(
            family,
            member,
            "现金后台测试账户",
        )
        security = Security.objects.create(
            symbol="CASH-ADMIN",
            name="现金后台测试标的",
            market="HK",
            currency="HKD",
        )
        transaction_item = InvestmentTransaction.objects.create(
            account=self.account,
            security=security,
            trade_date=date(2026, 7, 18),
            trade_type=TradeTypeChoices.BUY,
            quantity=Decimal("10"),
            price=Decimal("10"),
            amount=Decimal("100"),
            fee=Decimal("1"),
            currency="HKD",
        )
        rebuild_position(self.account, security)
        self.linked = InvestmentCashMovement.objects.get(
            transaction=transaction_item
        )
        self.independent = InvestmentCashMovement.objects.create(
            account=self.account,
            movement_date=date(2026, 7, 18),
            movement_type=CashMovementTypeChoices.DEPOSIT,
            currency="HKD",
            amount=Decimal("1000"),
        )
        self.request = RequestFactory().get(
            reverse("admin:portfolio_investmentcashmovement_changelist")
        )
        self.request.user = self.user
        self.model_admin = admin.site._registry[InvestmentCashMovement]

    def test_linked_movement_is_read_only_and_independent_movement_is_editable(self):
        self.assertFalse(
            self.model_admin.has_change_permission(self.request, self.linked)
        )
        self.assertFalse(
            self.model_admin.has_delete_permission(self.request, self.linked)
        )
        self.assertTrue(
            self.model_admin.has_change_permission(self.request, self.independent)
        )
        self.assertTrue(
            self.model_admin.has_delete_permission(self.request, self.independent)
        )
        self.assertIn(
            "amount",
            self.model_admin.get_readonly_fields(self.request, self.linked),
        )
        self.assertNotIn(
            "amount",
            self.model_admin.get_readonly_fields(self.request, self.independent),
        )

    def test_admin_links_to_transaction_and_disables_bulk_delete(self):
        link = str(self.model_admin.transaction_link(self.linked))

        self.assertIn(
            reverse(
                "admin:portfolio_investmenttransaction_change",
                args=[self.linked.transaction_id],
            ),
            link,
        )
        self.assertNotIn("delete_selected", self.model_admin.get_actions(self.request))

    def test_admin_only_offers_independent_cash_movement_types(self):
        field = self.model_admin.formfield_for_choice_field(
            InvestmentCashMovement._meta.get_field("movement_type"),
            self.request,
        )
        choices = {value for value, _ in field.choices}

        self.assertIn(CashMovementTypeChoices.DEPOSIT, choices)
        self.assertIn(CashMovementTypeChoices.EXCHANGE, choices)
        self.assertNotIn(CashMovementTypeChoices.BUY, choices)
        self.assertNotIn(CashMovementTypeChoices.SELL, choices)


class InvestmentCashMovementAuditCommandTests(TestCase):
    def test_audit_is_read_only_and_repair_rebuilds_transaction_movement(self):
        family = Family.objects.create(name="现金流水审计家庭")
        member = FamilyMember.objects.create(family=family, display_name="审计成员")
        account = create_broker_investment_account(family, member, "审计账户")
        security = Security.objects.create(
            symbol="CASH-AUDIT",
            name="现金流水审计标的",
            market="HK",
            currency="HKD",
        )
        transaction_item = InvestmentTransaction.objects.create(
            account=account,
            security=security,
            trade_date=date(2026, 7, 18),
            trade_type=TradeTypeChoices.BUY,
            quantity=Decimal("10"),
            price=Decimal("10"),
            amount=Decimal("100"),
            fee=Decimal("1"),
            currency="HKD",
        )
        rebuild_position(account, security)
        movement = InvestmentCashMovement.objects.get(transaction=transaction_item)
        InvestmentCashMovement.objects.filter(pk=movement.pk).update(
            amount=Decimal("999")
        )

        audit_output = StringIO()
        call_command("audit_investment_cash_movements", stdout=audit_output)
        movement.refresh_from_db()
        self.assertIn("不一致=1", audit_output.getvalue())
        self.assertEqual(movement.amount, Decimal("999"))

        repair_output = StringIO()
        call_command(
            "audit_investment_cash_movements",
            repair=True,
            stdout=repair_output,
        )
        movement.refresh_from_db()
        self.assertEqual(movement.amount, Decimal("-101"))
        self.assertIn("修复完成", repair_output.getvalue())

        final_output = StringIO()
        call_command("audit_investment_cash_movements", stdout=final_output)
        self.assertIn("缺失=0 不一致=0 多余=0", final_output.getvalue())


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
            display_name="我",
            display_order=1,
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
        self.assertContains(detail, "变动后现金余额")
        self.assertNotContains(detail, "变动后本位币余额")

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

    def test_accounts_split_by_member_and_collapse_balances_below_1000_cny(self):
        my_high = create_broker_investment_account(
            self.account.family,
            self.account.member,
            "我的高资产账户",
            currency="CNY",
            cash_balance=Decimal("20000"),
        )
        my_low = create_broker_investment_account(
            self.account.family,
            self.account.member,
            "我的低金额账户",
            currency="CNY",
            cash_balance=Decimal("999"),
        )
        secretary = FamilyMember.objects.create(
            family=self.account.family,
            display_name="孙秘书",
            display_order=2,
        )
        secretary_high = create_broker_investment_account(
            self.account.family,
            secretary,
            "秘书高资产账户",
            currency="CNY",
            cash_balance=Decimal("15000"),
        )
        secretary_low = create_broker_investment_account(
            self.account.family,
            secretary,
            "秘书低金额账户",
            currency="CNY",
            cash_balance=Decimal("500"),
        )

        response = self.client.get(reverse("portfolio:account_list"))

        groups = response.context["account_groups"]
        self.assertEqual([group["title"] for group in groups], ["我的投资账户", "孙秘书的投资账户"])
        self.assertEqual(
            [row["account"] for row in groups[0]["visible_rows"]],
            [my_high, self.account],
        )
        self.assertEqual(
            [row["account"] for row in groups[0]["collapsed_rows"]],
            [my_low],
        )
        self.assertEqual(
            [row["account"] for row in groups[1]["visible_rows"]],
            [secretary_high],
        )
        self.assertEqual(
            [row["account"] for row in groups[1]["collapsed_rows"]],
            [secretary_low],
        )
        self.assertContains(
            response,
            'class="account-prototype-table account-list-table member-account-table"',
            count=2,
        )
        self.assertContains(response, 'class="low-value-account-rows" hidden', count=2)

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

    def test_year_filter_only_lists_snapshot_years_and_uses_year_end_snapshot(self):
        for snapshot_date, cash in (
            (date(2024, 12, 31), Decimal("8000")),
            (date(2026, 7, 10), Decimal("12000")),
        ):
            snapshot = PortfolioSnapshot.objects.create(
                family=self.account.family,
                snapshot_date=snapshot_date,
                total_cash=cash,
                total_asset=cash,
                currency="CNY",
            )
            PortfolioSnapshotPositionLine.objects.create(
                snapshot=snapshot,
                account=self.account,
                asset_type="cash",
                asset_name="现金",
                currency="CNY",
                market_value_original=cash,
                market_value=cash,
            )

        latest = self.client.get(reverse("portfolio:account_list"), {"year": "all"})
        historical = self.client.get(reverse("portfolio:account_list"), {"year": "2024"})

        self.assertEqual(latest.context["year_options"], [2026, 2024])
        self.assertNotContains(latest, "2025 年")
        self.assertEqual(latest.context["account_rows"][0]["cash"], Decimal("10000"))
        self.assertEqual(historical.context["account_rows"][0]["cash"], Decimal("8000"))

    def test_zero_positions_are_hidden_and_profit_tab_renders(self):
        security = Security.objects.create(
            symbol="ZERO",
            name="零持仓",
            market="US",
            currency="USD",
        )
        InvestmentPosition.objects.create(
            account=self.account,
            security=security,
            quantity=0,
            position_date=date(2026, 7, 5),
        )

        positions = self.client.get(
            reverse("portfolio:account_detail", args=[self.account.pk]),
            {"tab": "positions"},
        )
        profit = self.client.get(
            reverse("portfolio:account_detail", args=[self.account.pk]),
            {"tab": "individual-profit"},
        )

        self.assertNotContains(positions, "零持仓")
        self.assertContains(profit, "个股盈亏")
        self.assertNotContains(profit, "统计年份")

    def test_account_realized_pnl_includes_fully_closed_securities(self):
        security = Security.objects.create(
            symbol="CLOSED",
            name="已清仓标的",
            market="CN",
            currency="CNY",
        )
        InvestmentPosition.objects.create(
            account=self.account,
            security=security,
            quantity=0,
            realized_pnl=Decimal("539.23"),
            position_date=date(2026, 7, 5),
        )
        InvestmentTransaction.objects.create(
            account=self.account,
            security=security,
            trade_date=date(2026, 7, 5),
            trade_type=TradeTypeChoices.SELL,
            quantity=1,
            price=1,
            amount=1,
            currency="CNY",
            realized_pnl=Decimal("539.23"),
        )

        dashboard = self.client.get(reverse("portfolio:account_list"))
        profit = self.client.get(
            reverse("portfolio:account_detail", args=[self.account.pk]),
            {"tab": "individual-profit"},
        )

        self.assertEqual(dashboard.context["account_rows"][0]["realized"], Decimal("539.23"))
        self.assertEqual(profit.context["individual_profit"]["rows"][0]["total_pnl"], Decimal("539.23"))
        self.assertContains(
            dashboard,
            '<span class="currency-symbol">¥</span><span class="currency-number">+539</span>',
            html=True,
        )

    def test_option_profit_breakdown_renders_values_in_table_columns(self):
        underlying = Security.objects.create(
            symbol="TSLA", name="特斯拉", market="US", currency="CNY"
        )
        option = Security.objects.create(
            symbol="TSLA 20260717 PUT 300.0",
            name="特斯拉看跌期权",
            market="US",
            asset_type=Security.TYPE_OPTION,
            currency="CNY",
        )
        OptionContract.objects.create(
            security=option,
            underlying=underlying,
            option_type=OptionContract.PUT,
            strike_price=300,
            expiration_date=date(2026, 7, 17),
        )
        InvestmentTransaction.objects.create(
            account=self.account,
            security=option,
            trade_date=date(2026, 7, 1),
            trade_type=TradeTypeChoices.SELL,
            position_effect=InvestmentTransaction.EFFECT_OPEN,
            quantity=1,
            price=5,
            amount=500,
            fee=1,
            currency="CNY",
        )
        InvestmentTransaction.objects.create(
            account=self.account,
            security=option,
            trade_date=date(2026, 7, 8),
            trade_type=TradeTypeChoices.BUY,
            position_effect=InvestmentTransaction.EFFECT_CLOSE,
            quantity=1,
            price=2,
            amount=200,
            fee=1,
            currency="CNY",
        )
        rebuild_position(self.account, option)

        response = self.client.get(
            reverse("portfolio:account_detail", args=[self.account.pk]),
            {"tab": "individual-profit", "currency": "CNY"},
        )
        child = response.context["individual_profit"]["rows"][0]["contracts"][0]

        self.assertEqual(child["display_total_pnl"], Decimal("298"))
        self.assertEqual(child["display_income"], Decimal("0"))
        self.assertContains(response, 'class="option-contract-row"')
        self.assertContains(response, "TSLA 20260717 PUT 300.0")

    def test_profit_total_sorting_and_transaction_filters_use_display_currency(self):
        ExchangeRate.objects.create(
            base_currency="USD",
            quote_currency="CNY",
            rate=Decimal("7"),
            rate_date=date(2026, 1, 1),
        )
        ExchangeRate.objects.create(
            base_currency="HKD",
            quote_currency="CNY",
            rate=Decimal("0.9"),
            rate_date=date(2026, 1, 1),
        )
        usd = Security.objects.create(
            symbol="USD-PROFIT", name="美元盈利", market="US", currency="USD"
        )
        hkd = Security.objects.create(
            symbol="HKD-PROFIT", name="港币盈利", market="HK", currency="HKD"
        )
        InvestmentTransaction.objects.create(
            account=self.account,
            security=usd,
            trade_date=date(2026, 2, 1),
            trade_type=TradeTypeChoices.SELL,
            quantity=1,
            price=1,
            amount=1,
            currency="USD",
            realized_pnl=Decimal("200"),
        )
        InvestmentTransaction.objects.create(
            account=self.account,
            security=hkd,
            trade_date=date(2026, 3, 1),
            trade_type=TradeTypeChoices.SELL,
            quantity=1,
            price=1,
            amount=1,
            currency="HKD",
            realized_pnl=Decimal("1000"),
        )

        profit = self.client.get(
            reverse("portfolio:account_detail", args=[self.account.pk]),
            {"tab": "individual-profit", "currency": "CNY"},
        )
        filtered = self.client.get(
            reverse("portfolio:account_detail", args=[self.account.pk]),
            {
                "tab": "transactions",
                "currency": "CNY",
                "date_start": "2026-02-01",
                "date_end": "2026-02-28",
                "stock": f"security:{usd.pk}",
            },
        )

        rows = profit.context["individual_profit"]["rows"]
        self.assertEqual([row["name"] for row in rows], ["美元盈利", "港币盈利"])
        self.assertEqual(
            profit.context["individual_profit"]["display_total_pnl"],
            sum((row["display_total_pnl"] for row in rows), Decimal("0")),
        )
        self.assertContains(profit, "合计")
        self.assertEqual(len(filtered.context["transactions"]), 1)
        self.assertEqual(filtered.context["transactions"][0].security, usd)
        self.assertContains(filtered, "开始日期")

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
        self.assertContains(response, "腾讯控股 - 成员甲 - 港股账户")
        self.assertEqual(response.context["total_asset"], Decimal("7200"))
        self.assertEqual(response.context["base_currency"], "CNY")
        self.assertEqual(PortfolioSnapshot.objects.count(), 0)

    def test_overview_never_mixes_financial_instrument_names_into_allocation_categories(self):
        response = self.client.get(reverse("portfolio:overview"))

        group_names = {item["name"] for item in response.context["asset_groups"]}
        self.assertIn("权益类", group_names)
        self.assertNotIn("股票", group_names)

    def test_manual_bond_uses_clean_price_plus_accrued_interest(self):
        Currency.objects.get_or_create(code="CNY", defaults={"name": "人民币"})
        fixed_income = AssetCategory.objects.create(
            family=self.account.family, name="固定收益类", code="fixed_income"
        )
        response = self.client.post(
            reverse("portfolio:bond_create"),
            {
                "asset_category": "",
                "symbol": "UST2030",
                "name": "美国国债 2030",
                "market": "US",
                "currency": "CNY",
                "isin": "US0000000001",
                "issuer": "美国财政部",
                "bond_type": BondDetail.GOVERNMENT,
                "face_value": "100",
                "coupon_rate": "4.25",
                "coupon_frequency": "2",
                "maturity_date": "2030-06-30",
                "redemption_price": "100",
                "quote_basis": BondDetail.PER_100,
                "clean_price": "98",
                "accrued_interest": "1.5",
                "valuation_date": "2026-07-12",
            },
        )
        self.assertEqual(response.status_code, 302)
        bond = Security.objects.get(symbol="UST2030", market="US")
        InvestmentPosition.objects.create(
            account=self.account,
            security=bond,
            quantity=Decimal("1000"),
            avg_cost=Decimal("97"),
            current_price=Decimal("98"),
            position_date=date(2026, 7, 12),
        )

        overview = self.client.get(reverse("portfolio:overview"))
        position = next(
            item for item in overview.context["asset_groups"]
            if item["name"] == "固定收益类"
        )

        self.assertEqual(position["amount"], Decimal("995"))
        self.assertEqual(bond.bond_detail.accrued_interest, Decimal("1.5"))
        self.assertEqual(bond.asset_category, fixed_income)

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

    def test_overview_defaults_to_all_family_members(self):
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

        response = self.client.get(reverse("portfolio:overview"))

        self.assertIsNone(response.context["selected_member"])
        self.assertEqual(response.context["selected_member_value"], "all")
        self.assertEqual(response.context["total_asset"], Decimal("7700"))
        self.assertContains(response, "全部家庭成员")

    def test_overview_uses_existing_snapshots_for_trend(self):
        PortfolioSnapshot.objects.create(
            family=self.member.family,
            member=None,
            snapshot_date=date(2026, 7, 4),
            total_asset=Decimal("5000"),
            currency="CNY",
            extra_data={"complete": True},
        )

        response = self.client.get(reverse("portfolio:overview"))

        self.assertEqual(len(response.context["trend_snapshots"]), 1)
        self.assertEqual(response.context["change_amount"], Decimal("0"))
        self.assertTrue(response.context["trend_points"])

    def test_overview_snapshot_views_use_period_end_and_daily_keeps_all_points(self):
        snapshot_values = (
            (date(2025, 12, 31), Decimal("100000")),
            (date(2026, 1, 15), Decimal("150000")),
            (date(2026, 1, 31), Decimal("200000")),
            (date(2026, 2, 1), Decimal("300000")),
        )
        for snapshot_date, total_asset in snapshot_values:
            PortfolioSnapshot.objects.create(
                family=self.member.family,
                member=None,
                snapshot_date=snapshot_date,
                total_asset=total_asset,
                currency="CNY",
                extra_data={"complete": True},
            )

        monthly = self.client.get(reverse("portfolio:overview"))
        yearly = self.client.get(
            reverse("portfolio:overview"), {"snapshot_view": "yearly"}
        )
        daily = self.client.get(
            reverse("portfolio:overview"), {"snapshot_view": "daily"}
        )

        self.assertEqual(
            [item.snapshot_date for item in monthly.context["trend_snapshots"]],
            [date(2025, 12, 31), date(2026, 1, 31), date(2026, 2, 1)],
        )
        self.assertEqual(
            [item.snapshot_date for item in yearly.context["trend_snapshots"]],
            [date(2025, 12, 31), date(2026, 2, 1)],
        )
        self.assertEqual(len(daily.context["trend_snapshots"]), 4)
        self.assertEqual(daily.context["chart_ticks"][0]["label"], "0.00")
        self.assertEqual(daily.context["trend_rows"][-1]["amount_label"], "30.00万")

    def test_daily_snapshot_chart_expands_instead_of_compressing_many_nodes(self):
        for day in range(1, 13):
            PortfolioSnapshot.objects.create(
                family=self.member.family,
                member=None,
                snapshot_date=date(2026, 6, day),
                total_asset=Decimal(day * 10000),
                currency="CNY",
                extra_data={"complete": True},
            )

        response = self.client.get(
            reverse("portfolio:overview"), {"snapshot_view": "daily"}
        )

        self.assertEqual(len(response.context["trend_rows"]), 12)
        self.assertGreater(response.context["chart_width"], 1000)
        self.assertContains(response, "可左右滚动查看")


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

        self.assertEqual(PortfolioSnapshot.objects.count(), 3)
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


class HistoricalPortfolioSnapshotTests(TestCase):
    def test_snapshot_rebuilds_position_as_of_date_instead_of_using_current_position(self):
        family = Family.objects.create(name="历史快照家庭")
        member = FamilyMember.objects.create(family=family, display_name="成员")
        account = create_broker_investment_account(
            family,
            member,
            "历史账户",
            currency="CNY",
            cash_balance=Decimal("1000"),
        )
        security = Security.objects.create(
            symbol="HISTORY",
            name="历史持仓测试",
            market="CN",
            asset_type=Security.TYPE_STOCK,
            currency="CNY",
        )
        InvestmentTransaction.objects.create(
            account=account,
            security=security,
            trade_date=date(2026, 6, 1),
            trade_type=TradeTypeChoices.BUY,
            quantity=Decimal("10"),
            price=Decimal("10"),
            amount=Decimal("100"),
            currency="CNY",
        )
        InvestmentTransaction.objects.create(
            account=account,
            security=security,
            trade_date=date(2026, 7, 1),
            trade_type=TradeTypeChoices.SELL,
            quantity=Decimal("5"),
            price=Decimal("20"),
            amount=Decimal("100"),
            currency="CNY",
        )
        current_position = rebuild_position(account, security)
        self.assertEqual(current_position.quantity, Decimal("5"))
        SecurityPriceRecord.objects.create(
            security=security,
            price=Decimal("15"),
            currency="CNY",
            source=PriceSourceChoices.MANUAL,
            price_as_of=timezone.make_aware(
                datetime(2026, 6, 30, 16, 0)
            ),
        )

        snapshots = create_portfolio_snapshots_for_date(
            family,
            [account],
            date(2026, 6, 30),
            "CNY",
            require_complete=True,
        )

        family_snapshot = next(
            item for item in snapshots if item.member_id is None
        )
        position_line = family_snapshot.position_lines.get(security=security)
        self.assertEqual(position_line.quantity, Decimal("10"))
        self.assertEqual(position_line.market_value, Decimal("150"))
        self.assertEqual(family_snapshot.total_cash, Decimal("900"))
        self.assertEqual(family_snapshot.total_asset, Decimal("1050"))


class PortfolioReconciliationTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="reconciliation-admin",
            password="test-password",
            is_staff=True,
        )
        self.family = Family.objects.create(name="差额对齐家庭")
        self.member = FamilyMember.objects.create(
            family=self.family,
            display_name="管理员",
            role=FamilyMember.ROLE_ADMIN,
            user=self.user,
        )
        self.account = create_broker_investment_account(
            self.family,
            self.member,
            "对齐账户",
            currency="CNY",
            cash_balance=Decimal("1000"),
        )
        self.ledger_snapshot = AssetBalanceSnapshot.objects.create(
            family=self.family,
            snapshot_date=date(2026, 6, 30),
            base_currency="CNY",
            is_draft=False,
        )
        self.entry = AssetBalanceEntry.objects.create(
            snapshot=self.ledger_snapshot,
            member=self.member,
            account=self.account.bank_account,
            currency="CNY",
            original_amount=Decimal("1200"),
            base_amount=Decimal("1200"),
        )

    def test_apply_is_idempotent_updates_existing_adjustment_and_can_revert(self):
        preview = build_reconciliation_preview(self.ledger_snapshot)
        self.assertTrue(preview.can_apply)
        self.assertEqual(preview.ready_rows[0].adjustment_base_amount, Decimal("200"))

        first = apply_reconciliation(self.ledger_snapshot)
        second = apply_reconciliation(self.ledger_snapshot)

        self.assertEqual(first.pk, second.pk)
        movements = InvestmentCashMovement.objects.filter(
            source=TransactionSourceChoices.RECONCILIATION
        )
        self.assertEqual(movements.count(), 1)
        self.assertEqual(movements.get().amount, Decimal("200"))
        snapshot = PortfolioSnapshot.objects.get(
            family=self.family,
            member=None,
            account=None,
            snapshot_date=date(2026, 6, 30),
            currency="CNY",
        )
        self.assertEqual(snapshot.total_asset, Decimal("1200"))

        self.entry.original_amount = Decimal("1250")
        self.entry.base_amount = Decimal("1250")
        self.entry.save(update_fields=["original_amount", "base_amount"])
        apply_reconciliation(self.ledger_snapshot)
        self.assertEqual(movements.get().amount, Decimal("250"))

        run = PortfolioReconciliationRun.objects.get(pk=first.pk)
        revert_reconciliation(run)
        self.assertFalse(
            InvestmentCashMovement.objects.filter(
                source=TransactionSourceChoices.RECONCILIATION
            ).exists()
        )
        snapshot.refresh_from_db()
        self.assertEqual(snapshot.total_asset, Decimal("1000"))

    def test_adjustment_does_not_change_earlier_snapshot(self):
        apply_reconciliation(self.ledger_snapshot)
        earlier = create_portfolio_snapshots_for_date(
            self.family,
            [self.account],
            date(2026, 5, 31),
            "CNY",
            require_complete=True,
        )[0]
        self.assertEqual(earlier.total_asset, Decimal("1000"))

    def test_admin_can_render_reconciliation_preview_page(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("portfolio:reconciliation_preview"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "投资账户月底差额对齐")
        self.assertContains(response, "200.00")


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
        self.assertContains(
            response,
            'class="positive"><span class="number-value">500.20</span>',
        )
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


class MarketDataFoundationTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="market-data-tester",
            password="test-password",
        )
        self.family = Family.objects.create(name="行情测试家庭")
        self.member = FamilyMember.objects.create(
            family=self.family,
            user=self.user,
            display_name="行情测试成员",
        )
        self.account = create_broker_investment_account(
            self.family,
            self.member,
            "行情测试账户",
        )
        self.security = Security.objects.create(
            symbol="00700",
            name="腾讯控股",
            market="HK",
            exchange="HK",
            asset_type=Security.TYPE_STOCK,
            currency="HKD",
        )
        self.position = InvestmentPosition.objects.create(
            account=self.account,
            security=self.security,
            quantity=Decimal("10"),
            avg_cost=Decimal("400"),
            current_price=Decimal("400"),
            position_date=date.today(),
        )
        self.client.force_login(self.user)

    @patch("portfolio.market_data.get_futu_market_snapshots")
    def test_batch_refresh_writes_history_latest_quote_and_position_value(self, fetch):
        fetch.return_value = {
            "HK.00700": {
                "last_price": "500",
                "quote_time": timezone.localtime().strftime("%Y-%m-%d %H:%M:%S"),
                "change_rate": "2.5",
                "raw_data": {"sec_status": "NORMAL"},
            }
        }

        run = refresh_market_data(security_ids=[self.security.pk])

        self.assertEqual(run.status, MarketDataRunStatusChoices.SUCCESS)
        self.assertEqual(run.success_count, 1)
        snapshot = SecurityMarketSnapshot.objects.get(security=self.security)
        self.assertEqual(snapshot.last_price, Decimal("500"))
        self.assertEqual(snapshot.price_source, PriceSourceChoices.FUTU)
        self.assertEqual(SecurityPriceRecord.objects.count(), 1)
        self.position.refresh_from_db()
        self.assertEqual(self.position.current_price, Decimal("500"))
        self.assertEqual(self.position.market_value, Decimal("5000"))
        self.assertEqual(self.position.pricing_status, PricingStatusChoices.FRESH)

    @patch("portfolio.market_data.get_futu_market_snapshots")
    def test_failed_refresh_preserves_last_price_and_marks_error(self, fetch):
        record_security_price(
            self.security,
            Decimal("480"),
            source=PriceSourceChoices.FUTU,
            price_as_of=timezone.now(),
        )
        fetch.side_effect = FutuQueryError("OpenD 暂不可用")

        run = refresh_market_data(security_ids=[self.security.pk])

        self.assertEqual(run.status, MarketDataRunStatusChoices.FAILED)
        snapshot = SecurityMarketSnapshot.objects.get(security=self.security)
        self.assertEqual(snapshot.last_price, Decimal("480"))
        self.assertEqual(snapshot.pricing_status, PricingStatusChoices.ERROR)
        self.position.refresh_from_db()
        self.assertEqual(self.position.current_price, Decimal("480"))
        self.assertEqual(self.position.pricing_status, PricingStatusChoices.ERROR)

    def test_market_data_get_is_read_only_and_manual_price_updates_valuation(self):
        response = self.client.get(reverse("portfolio:market_data_status"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "行情与估值")
        self.assertFalse(SecurityQuoteConfig.objects.exists())

        response = self.client.post(
            reverse("portfolio:manual_security_price", args=[self.security.pk]),
            {
                "price": "510.25",
                "price_as_of": timezone.localtime().strftime("%Y-%m-%dT%H:%M"),
                "remark": "测试手工收盘价",
            },
        )

        self.assertRedirects(response, reverse("portfolio:market_data_status"))
        self.assertTrue(SecurityQuoteConfig.objects.filter(security=self.security).exists())
        latest = SecurityPriceRecord.objects.get(security=self.security)
        self.assertEqual(latest.source, PriceSourceChoices.MANUAL)
        self.position.refresh_from_db()
        self.assertEqual(self.position.current_price, Decimal("510.25"))
        self.assertEqual(self.position.market_value, Decimal("5102.5"))
        self.assertEqual(self.position.pricing_status, PricingStatusChoices.MANUAL)

    def test_bond_uses_per_100_quote_and_expired_option_is_not_forced_to_zero(self):
        bond = Security.objects.create(
            symbol="UST-TEST",
            name="测试美债",
            market="US",
            asset_type=Security.TYPE_BOND,
            currency="USD",
        )
        BondDetail.objects.create(
            security=bond,
            quote_basis=BondDetail.PER_100,
            accrued_interest=Decimal("2"),
        )
        bond_position = InvestmentPosition.objects.create(
            account=self.account,
            security=bond,
            quantity=Decimal("1000"),
            avg_cost=Decimal("95"),
            current_price=Decimal("0"),
            position_date=date.today(),
        )
        record_security_price(
            bond,
            Decimal("98"),
            source=PriceSourceChoices.MANUAL,
            price_as_of=timezone.now(),
            price_type="manual",
        )

        underlying = Security.objects.create(
            symbol="TSLA",
            name="特斯拉",
            market="US",
            currency="USD",
        )
        option = Security.objects.create(
            symbol="TSLA-EXPIRED-PUT",
            name="到期看跌期权",
            market="US",
            asset_type=Security.TYPE_OPTION,
            currency="USD",
        )
        OptionContract.objects.create(
            security=option,
            underlying=underlying,
            option_type=OptionContract.PUT,
            strike_price=Decimal("300"),
            expiration_date=date.today() - timedelta(days=1),
            multiplier=100,
        )
        option_position = InvestmentPosition.objects.create(
            account=self.account,
            security=option,
            quantity=Decimal("1"),
            avg_cost=Decimal("4"),
            current_price=Decimal("5"),
            position_date=date.today(),
        )

        refresh_position_valuations(security_ids=[bond.pk, option.pk])

        bond_position.refresh_from_db()
        self.assertEqual(bond_position.market_value, Decimal("1000"))
        self.assertEqual(bond_position.unrealized_pnl, Decimal("50"))
        option_position.refresh_from_db()
        self.assertEqual(option_position.current_price, Decimal("5"))
        self.assertEqual(option_position.market_value, Decimal("500"))
        self.assertEqual(
            option_position.pricing_status,
            PricingStatusChoices.EXPIRED_UNRESOLVED,
        )


class SecurityMarketDictionaryTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="security-market-tester",
            password="test-password",
        )
        self.family = Family.objects.create(name="市场字典测试家庭")
        FamilyMember.objects.create(
            family=self.family,
            user=self.user,
            display_name="市场字典成员",
        )
        AssetCategory.objects.create(
            family=self.family,
            name="权益类",
            code="equity",
        )
        Currency.objects.update_or_create(
            code="USD",
            defaults={"name": "美元", "symbol": "$", "is_active": True},
        )
        Currency.objects.update_or_create(
            code="HKD",
            defaults={"name": "港币", "symbol": "HK$", "is_active": True},
        )
        self.client.force_login(self.user)

    def test_seeded_b_share_dictionary_and_valid_security_creation(self):
        b_market = SecurityMarket.objects.get(code="CN_B")
        self.assertEqual(b_market.name, "B 股")
        self.assertEqual(
            set(
                SecurityExchange.objects.filter(market=b_market).values_list(
                    "code",
                    "default_currency",
                    "provider_prefix",
                )
            ),
            {("SH", "USD", "SH"), ("SZ", "HKD", "SZ")},
        )

        response = self.client.post(
            reverse("portfolio:security_create"),
            {
                "asset_category": "",
                "symbol": "900901",
                "name": "测试上海 B 股",
                "market": "CN_B",
                "exchange": "CN_B:SH",
                "asset_type": Security.TYPE_STOCK,
                "currency": "USD",
                "industry": "",
                "lot_size": "100",
                "listing_date": "",
                "is_active": "on",
            },
        )

        self.assertRedirects(response, reverse("portfolio:security_list"))
        security = Security.objects.get(symbol="900901", market="CN_B")
        self.assertEqual(security.exchange, "SH")
        self.assertEqual(security.currency, "USD")
        config = SecurityQuoteConfig.objects.get(security=security)
        self.assertEqual(config.provider, PriceSourceChoices.FUTU)
        self.assertEqual(config.provider_symbol, "SH.900901")

    def test_b_share_code_and_currency_are_validated(self):
        response = self.client.post(
            reverse("portfolio:security_create"),
            {
                "asset_category": "",
                "symbol": "600000",
                "name": "错误 B 股",
                "market": "CN_B",
                "exchange": "CN_B:SH",
                "asset_type": Security.TYPE_STOCK,
                "currency": "HKD",
                "industry": "",
                "lot_size": "100",
                "listing_date": "",
                "is_active": "on",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "代码应以 900 开头")
        self.assertContains(response, "应使用 USD 计价")
        self.assertFalse(Security.objects.filter(name="错误 B 股").exists())

    def test_admin_maintained_market_and_exchange_appear_in_form(self):
        market = SecurityMarket.objects.create(
            code="JP",
            name="日本股市",
            default_currency="JPY",
            display_order=50,
        )
        SecurityExchange.objects.create(
            market=market,
            code="TSE",
            name="东京证券交易所",
            default_currency="JPY",
            display_order=10,
        )

        response = self.client.get(reverse("portfolio:security_create"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'value="JP"')
        self.assertContains(response, 'value="JP:TSE"')

        market.code = "JP_CHANGED"
        with self.assertRaisesMessage(ValidationError, "稳定代码创建后不可修改"):
            market.save()
