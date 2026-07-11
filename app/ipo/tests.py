from datetime import date, timedelta
from decimal import Decimal
import io
import json
import os
import urllib.error
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.template.loader import render_to_string
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from ai_analysis.models import AiProvider
from family_core.models import AccountType, Family, FamilyMember
from ledger.models import BankAccount
from portfolio.models import InvestmentTransaction, TradeTypeChoices

from .forms import HkIpoAllotmentForm, HkIpoListingForm, HkIpoSubscriptionTradeForm
from .models import HkIpoListing, HkIpoListingOption, HkIpoSubscriptionTrade
from .services import (
    IpoImageRecognitionError,
    _hk_connect_threshold_cache,
    _vbkr_margin_cache,
    build_prompt,
    fetch_hk_connect_threshold_100m,
    fetch_jesselivermore_ipo_metrics,
    fetch_vbkr_expected_margin_multiples,
    get_active_vision_provider,
    get_api_key,
    normalize_value,
    recognize_ipo_listing_from_image,
    refresh_listed_market_data,
)
from .views import subscription_trade_list_url


class HkIpoSubscriptionTradeCalculationTests(TestCase):
    def setUp(self):
        self.family = Family.objects.create(name="Test Family")
        self.member = FamilyMember.objects.create(family=self.family, display_name="Tester")
        self.account = BankAccount.objects.create(
            family=self.family,
            member=self.member,
            account_name="IPO Account",
            remark="打新账户",
            supports_ipo=True,
        )
        self.listing = HkIpoListing.objects.create(
            stock_code="09999.HK",
            stock_name="测试新股",
            company_name="测试新股有限公司",
            subscription_start_date=date(2026, 6, 1),
            subscription_end_date=date(2026, 6, 5),
            allotment_result_date=date(2026, 6, 8),
            listing_date=date(2026, 6, 9),
            final_price=Decimal("10"),
            lot_size=100,
            global_offer_shares_10k=Decimal("1000"),
        )

    def make_trade(self, **kwargs):
        defaults = {
            "listing": self.listing,
            "member": self.member,
            "account": self.account,
            "application_date": date(2026, 6, 2),
            "applied_lots": 2,
            "subscription_fee": Decimal("100"),
            "financing_interest": Decimal("10"),
            "trading_fee": Decimal("10"),
        }
        defaults.update(kwargs)
        return HkIpoSubscriptionTrade.objects.create(**defaults)

    def test_realized_profit_splits_allotment_fee_from_trading_fee(self):
        trade = self.make_trade(
            allotted_lots=2,
            sold_lots=2,
            sell_price=Decimal("12"),
            sell_date=date(2026, 6, 9),
        )

        expected_interest = Decimal("10")
        self.assertEqual(trade.allotted_value, Decimal("2000"))
        self.assertEqual(trade.allotment_fee, Decimal("20.00"))
        self.assertEqual(trade.trading_fee, Decimal("10"))
        self.assertEqual(trade.financing_interest, expected_interest)
        self.assertEqual(trade.trade_status, HkIpoSubscriptionTrade.STATUS_CLOSED)
        self.assertEqual(
            trade.realized_profit.quantize(Decimal("0.0001")),
            (Decimal("400") - Decimal("100") - expected_interest - Decimal("20") - Decimal("10")).quantize(Decimal("0.0001")),
        )

    def test_total_fees_and_holding_value(self):
        trade = self.make_trade(allotted_lots=2, sold_lots=1)

        self.assertEqual(
            trade.total_fees,
            trade.subscription_fee
            + trade.allotment_fee
            + trade.financing_interest
            + trade.trading_fee,
        )
        self.assertEqual(trade.holding_value, Decimal("2000"))
        self.assertEqual(trade.remaining_upfront_fees, Decimal("65"))
        self.assertEqual(trade.break_even_price, Decimal("10.65"))

    def test_subscription_form_uses_manual_financing_interest(self):
        form = HkIpoSubscriptionTradeForm()

        self.assertIn("financing_interest", form.fields)
        self.assertNotIn("financing_amount", form.fields)
        self.assertNotIn("financing_rate", form.fields)
        self.assertNotIn("financing_days", form.fields)

    def test_structural_fields_are_locked_after_a_sale(self):
        trade = self.make_trade(allotted_lots=2, sold_lots=1)

        application_form = HkIpoSubscriptionTradeForm(instance=trade)
        allotment_form = HkIpoAllotmentForm(instance=trade)

        for field_name in (
            "listing",
            "member",
            "account",
            "tranche",
            "applied_lots",
            "application_method",
        ):
            self.assertTrue(application_form.fields[field_name].disabled)
        self.assertFalse(application_form.fields["subscription_fee"].disabled)
        self.assertFalse(application_form.fields["financing_interest"].disabled)
        self.assertTrue(allotment_form.fields["allotted_lots"].disabled)

    def test_status_changes_from_applying_to_holding_to_closed(self):
        applying = self.make_trade(allotted_lots=None)
        holding = self.make_trade(allotted_lots=2, sold_lots=1, sell_date=date(2026, 6, 9))
        closed = self.make_trade(allotted_lots=2, sold_lots=2, sell_date=date(2026, 6, 9))

        self.assertEqual(applying.trade_status, HkIpoSubscriptionTrade.STATUS_APPLYING)
        self.assertEqual(holding.trade_status, HkIpoSubscriptionTrade.STATUS_HOLDING)
        self.assertEqual(closed.trade_status, HkIpoSubscriptionTrade.STATUS_CLOSED)

    def test_zero_allotted_lots_auto_sets_sell_date_to_allotment_result_date(self):
        trade = self.make_trade(allotted_lots=0, sold_lots=0)

        self.assertEqual(
            trade.trade_status,
            HkIpoSubscriptionTrade.STATUS_UNALLOTTED,
        )
        self.assertEqual(trade.get_trade_status_display(), "未中签")
        self.assertEqual(trade.sell_date, self.listing.allotment_result_date)

    def test_unallotted_closed_row_shows_all_upfront_fees_as_loss(self):
        user = get_user_model().objects.create_user(username="ipo-unallotted-fee-tester")
        self.member.user = user
        self.member.save(update_fields=["user"])
        trade = self.make_trade(allotted_lots=0, financing_interest=Decimal("5"))
        self.client.force_login(user)

        response = self.client.get(
            reverse("ipo:subscription_trade_list"),
            {"year": "2026"},
        )

        row = next(
            item
            for item in response.context["closed_visible"] + response.context["closed_hidden"]
            if item.ipo_trade.pk == trade.pk
        )
        self.assertEqual(row.display_total_fee, Decimal("105"))
        self.assertEqual(row.display_net_pnl, Decimal("-105"))
        self.assertEqual(trade.realized_profit, Decimal("-105"))
        self.assertFalse(trade.investment_transactions.exists())

    def test_subscription_page_uses_status_specific_amount_columns(self):
        user = get_user_model().objects.create_user(
            username="ipo-tester",
            password="test-password",
        )
        self.member.user = user
        self.member.save(update_fields=["user"])
        self.make_trade(allotted_lots=None)
        self.make_trade(allotted_lots=2, sold_lots=1)
        self.make_trade(
            allotted_lots=2,
            sold_lots=2,
            sell_price=Decimal("12"),
            sell_date=date(2026, 6, 9),
        )
        self.client.force_login(user)

        response = self.client.get(reverse("ipo:subscription_trade_list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "费用合计", count=2)
        self.assertContains(response, "持有货值", count=1)
        self.assertContains(response, "1,000.00")

    def test_partial_sale_is_shown_as_remaining_holding_and_closed_sale(self):
        user = get_user_model().objects.create_user(
            username="ipo-partial-sale-tester",
            password="test-password",
        )
        self.member.user = user
        self.member.save(update_fields=["user"])
        trade = self.make_trade(
            allotted_lots=2,
            sold_lots=1,
            sell_price=Decimal("12"),
            sell_date=date(2026, 6, 9),
        )
        self.client.force_login(user)

        response = self.client.get(
            reverse("ipo:subscription_trade_list"),
            {"year": "2026"},
        )

        holding_trades = response.context["holding_trades"]
        closed_trades = response.context["closed_visible"] + response.context["closed_hidden"]
        self.assertEqual([item.pk for item in holding_trades], [trade.pk])
        self.assertEqual([item.pk for item in closed_trades], [trade.pk])
        self.assertEqual(holding_trades[0].display_remaining_lots, 1)
        self.assertEqual(holding_trades[0].display_holding_profit, Decimal("-65"))
        self.assertEqual(holding_trades[0].display_status_label, "部分卖出")
        self.assertEqual(holding_trades[0].display_status_class, "partial")
        self.assertEqual(closed_trades[0].ipo_trade.allotted_lots, 2)
        self.assertEqual(closed_trades[0].display_sold_lots, 1)
        self.assertEqual(response.context["metrics"]["realized_profit_total"], trade.realized_profit)
        self.assertEqual(response.context["metrics"]["closed"], 1)

    def test_cancel_sale_transaction_returns_trade_to_holding(self):
        user = get_user_model().objects.create_user(
            username="ipo-cancel-sale-tester",
            password="test-password",
        )
        self.member.user = user
        self.member.save(update_fields=["user"])
        broker_type = AccountType.objects.create(family=self.family, name="券商")
        self.account.account_type_ref = broker_type
        self.account.save(update_fields=["account_type_ref"])
        trade = self.make_trade(allotted_lots=2)
        self.client.force_login(user)

        self.client.post(
            reverse("ipo:subscription_trade_sale", args=[trade.pk]),
            {
                "sell_price": "12",
                "sell_date": "2026-06-09",
                "sold_lots": "1",
                "trading_fee": "10",
            },
        )
        sale = InvestmentTransaction.objects.get(
            trade_type=TradeTypeChoices.SELL,
            ipo_subscription_trade=trade,
        )
        self.assertEqual(sale.fee, Decimal("10"))

        list_response = self.client.get(
            reverse("ipo:subscription_trade_list"),
            {"year": "2026"},
        )
        self.assertContains(
            list_response,
            reverse("ipo:subscription_trade_sale_cancel", args=[trade.pk, sale.pk]),
        )

        detail_response = self.client.get(
            reverse("ipo:subscription_trade_detail", args=[trade.pk])
        )
        displayed_sale = detail_response.context["sale_transactions"][0]
        self.assertEqual(displayed_sale.display_gross_pnl, Decimal("200"))
        self.assertEqual(displayed_sale.display_upfront_fee, Decimal("65"))
        self.assertEqual(displayed_sale.display_total_fee, Decimal("75"))
        self.assertEqual(displayed_sale.display_net_pnl, Decimal("125"))
        self.assertNotContains(
            detail_response,
            reverse("ipo:subscription_trade_allotment", args=[trade.pk]),
        )

        response = self.client.post(
            reverse("ipo:subscription_trade_sale_cancel", args=[trade.pk, sale.pk]),
        )

        trade.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertFalse(InvestmentTransaction.objects.filter(pk=sale.pk).exists())
        self.assertEqual(trade.sold_lots, 0)
        self.assertEqual(trade.trade_status, HkIpoSubscriptionTrade.STATUS_HOLDING)
        self.assertEqual(trade.realized_profit, Decimal("0"))

    def test_grey_market_sale_moves_buy_date_before_sale_rebuild(self):
        user = get_user_model().objects.create_user(username="ipo-grey-sale-tester")
        self.member.user = user
        self.member.save(update_fields=["user"])
        broker_type = AccountType.objects.create(family=self.family, name="券商")
        self.account.account_type_ref = broker_type
        self.account.save(update_fields=["account_type_ref"])
        trade = self.make_trade(allotted_lots=1)
        self.client.force_login(user)

        response = self.client.post(
            reverse("ipo:subscription_trade_sale", args=[trade.pk]),
            {
                "sell_price": "12",
                "sell_date": "2026-06-07",
                "sold_lots": "1",
                "trading_fee": "10",
            },
        )

        self.assertEqual(response.status_code, 302)
        buy = InvestmentTransaction.objects.get(
            trade_type=TradeTypeChoices.IPO,
            ipo_subscription_trade=trade,
        )
        self.assertEqual(buy.trade_date, date(2026, 6, 7))

    def test_closed_trade_table_uses_sale_columns(self):
        trade = self.make_trade(
            allotted_lots=2,
            sold_lots=2,
            sell_price=Decimal("12"),
            sell_date=date(2026, 6, 9),
        )
        trade.ipo_trade = trade
        trade.display_sold_lots = trade.sold_lots
        trade.price = trade.sell_price
        trade.trade_date = trade.sell_date
        trade.fee = trade.trading_fee
        trade.realized_pnl = trade.realized_profit
        trade.display_total_fee = trade.total_fees
        trade.display_net_pnl = trade.realized_profit
        trade.legacy_sale_row = True

        html = render_to_string(
            "ipo/_subscription_trade_table.html",
            {
                "trades": [trade],
                "closed_mode": True,
                "amount_column": "fees",
            },
        )

        self.assertNotIn("申购档位", html)
        self.assertNotIn("申购手数", html)
        self.assertNotIn("申购方式", html)
        self.assertIn("最终定价", html)
        self.assertIn("10.00", html)
        self.assertIn("卖出金额", html)
        self.assertIn("12.00", html)

    def test_closed_stock_metric_excludes_zero_allotment_records(self):
        user = get_user_model().objects.create_user(
            username="ipo-closed-metric-tester",
            password="test-password",
        )
        self.member.user = user
        self.member.save(update_fields=["user"])
        self.make_trade(
            allotted_lots=1,
            sold_lots=1,
            sell_price=Decimal("12"),
            sell_date=date(2026, 6, 9),
        )
        zero_allotment_listing = HkIpoListing.objects.create(
            stock_code="ZERO.US",
            stock_name="未中签测试",
            company_name="未中签测试有限公司",
            subscription_end_date=date(2026, 6, 5),
            allotment_result_date=date(2026, 6, 8),
            listing_date=date(2026, 6, 9),
            final_price=Decimal("10"),
            lot_size=100,
        )
        HkIpoSubscriptionTrade.objects.create(
            listing=zero_allotment_listing,
            member=self.member,
            account=self.account,
            application_date=date(2026, 6, 2),
            applied_lots=1,
            allotted_lots=0,
            sold_lots=0,
        )
        self.client.force_login(user)

        subscription_response = self.client.get(
            reverse("ipo:subscription_trade_list"),
            {"year": "2026"},
        )
        overview_response = self.client.get(reverse("ipo:index"))

        self.assertEqual(subscription_response.context["metrics"]["allotted"], 1)
        self.assertEqual(subscription_response.context["metrics"]["closed"], 1)
        self.assertEqual(overview_response.context["metrics"]["trade_closed"], 1)

    def test_closed_trades_use_sell_year_and_sell_date_descending(self):
        user = get_user_model().objects.create_user(
            username="ipo-sort-tester",
            password="test-password",
        )
        self.member.user = user
        self.member.save(update_fields=["user"])
        older = self.make_trade(
            application_date=date(2026, 6, 3),
            allotted_lots=1,
            sold_lots=1,
            sell_date=date(2026, 6, 8),
        )
        newer = self.make_trade(
            application_date=date(2026, 6, 1),
            allotted_lots=1,
            sold_lots=1,
            sell_date=date(2026, 6, 10),
        )
        historical_listing = HkIpoListing.objects.create(
            stock_code="HIST.US",
            stock_name="历史新股",
            company_name="历史新股有限公司",
            subscription_end_date=date(2025, 9, 10),
            allotment_result_date=date(2025, 9, 11),
            listing_date=date(2025, 9, 11),
            final_price=Decimal("10"),
            lot_size=10,
        )
        historical_trade = HkIpoSubscriptionTrade.objects.create(
            listing=historical_listing,
            member=self.member,
            account=self.account,
            application_date=date(2026, 6, 4),
            applied_lots=1,
            allotted_lots=1,
            sold_lots=1,
            sell_price=Decimal("12"),
            sell_date=date(2026, 6, 9),
        )
        self.client.force_login(user)

        response = self.client.get(
            reverse("ipo:subscription_trade_list"),
            {"year": "2026"},
        )

        closed_trades = (
            response.context["closed_visible"] + response.context["closed_hidden"]
        )
        self.assertEqual(
            [trade.pk for trade in closed_trades],
            [newer.pk, historical_trade.pk, older.pk],
        )

    def test_selected_year_persists_in_session(self):
        user = get_user_model().objects.create_user(
            username="ipo-year-tester",
            password="test-password",
        )
        self.member.user = user
        self.member.save(update_fields=["user"])
        self.make_trade(
            allotted_lots=1,
            sold_lots=1,
            sell_date=date(2025, 9, 12),
        )
        self.client.force_login(user)

        self.client.get(reverse("ipo:subscription_trade_list"), {"year": "2025"})
        self.client.get(reverse("ipo:index"))
        response = self.client.get(reverse("ipo:subscription_trade_list"))

        self.assertEqual(response.context["year_filter"]["selected_year"], "2025")

    def test_closed_trade_redirects_to_its_sell_year(self):
        trade = self.make_trade(
            allotted_lots=1,
            sold_lots=1,
            sell_date=date(2025, 9, 12),
        )

        self.assertEqual(
            subscription_trade_list_url(trade),
            f"{reverse('ipo:subscription_trade_list')}?year=2025",
        )

    def test_stock_profit_options_only_include_closed_trades_for_selected_year(self):
        user = get_user_model().objects.create_user(
            username="ipo-profit-options-tester",
            password="test-password",
        )
        self.member.user = user
        self.member.save(update_fields=["user"])
        selected_trade = self.make_trade(
            allotted_lots=1,
            sold_lots=1,
            sell_price=Decimal("12"),
            sell_date=date(2026, 6, 9),
        )
        self.make_trade(
            allotted_lots=1,
            sold_lots=1,
            sell_price=Decimal("11"),
            sell_date=date(2025, 6, 9),
        )
        newer_listing = HkIpoListing.objects.create(
            stock_code="NEWER.US",
            stock_name="较新卖出",
            company_name="较新卖出有限公司",
            subscription_end_date=date(2026, 6, 5),
            final_price=Decimal("10"),
            lot_size=100,
        )
        HkIpoSubscriptionTrade.objects.create(
            listing=newer_listing,
            member=self.member,
            account=self.account,
            application_date=date(2026, 6, 2),
            applied_lots=1,
            allotted_lots=1,
            sold_lots=1,
            sell_price=Decimal("12"),
            sell_date=date(2026, 6, 10),
        )
        holding_listing = HkIpoListing.objects.create(
            stock_code="HOLDING.US",
            stock_name="礼邦医药测试",
            company_name="礼邦医药测试有限公司",
            subscription_end_date=date(2026, 6, 5),
            final_price=Decimal("10"),
            lot_size=100,
        )
        HkIpoSubscriptionTrade.objects.create(
            listing=holding_listing,
            member=self.member,
            account=self.account,
            application_date=date(2026, 6, 2),
            applied_lots=1,
            allotted_lots=1,
            sold_lots=0,
        )
        self.client.force_login(user)

        response = self.client.get(
            reverse("ipo:subscription_trade_list"),
            {"year": "2026", "stock": str(self.listing.pk)},
        )

        options = list(response.context["profit_queries"]["stock_options"])
        self.assertEqual(
            [listing.pk for listing in options],
            [newer_listing.pk, self.listing.pk],
        )
        self.assertEqual(
            response.context["profit_queries"]["stock_profit_total"],
            selected_trade.realized_profit,
        )

    def test_account_profit_query_uses_selected_year_and_filters_closed_details(self):
        user = get_user_model().objects.create_user(
            username="ipo-account-profit-tester",
            password="test-password",
        )
        self.member.user = user
        self.member.save(update_fields=["user"])
        selected_trade = self.make_trade(
            allotted_lots=1,
            sold_lots=1,
            sell_price=Decimal("12"),
            sell_date=date(2026, 6, 9),
        )
        self.make_trade(
            allotted_lots=1,
            sold_lots=1,
            sell_price=Decimal("11"),
            sell_date=date(2025, 6, 9),
        )
        second_account = BankAccount.objects.create(
            family=self.family,
            member=self.member,
            account_name="Second IPO Account",
            remark="打新账户",
            supports_ipo=True,
        )
        HkIpoSubscriptionTrade.objects.create(
            listing=self.listing,
            member=self.member,
            account=second_account,
            application_date=date(2026, 6, 2),
            applied_lots=1,
            allotted_lots=1,
            sold_lots=1,
            sell_price=Decimal("13"),
            sell_date=date(2026, 6, 10),
        )
        self.client.force_login(user)

        response = self.client.get(
            reverse("ipo:subscription_trade_list"),
            {"year": "2026", "account": str(self.account.pk)},
        )

        options = list(response.context["profit_queries"]["account_options"])
        closed_trades = (
            response.context["closed_visible"] + response.context["closed_hidden"]
        )
        self.assertEqual(
            [account.pk for account in options],
            [self.account.pk, second_account.pk],
        )
        self.assertEqual(
            response.context["profit_queries"]["account_profit_total"],
            selected_trade.realized_profit,
        )
        self.assertEqual([trade.pk for trade in closed_trades], [selected_trade.pk])
        self.assertContains(response, "按账户查询")

    def test_overview_year_filter_drives_metrics_and_chart_series(self):
        user = get_user_model().objects.create_user(
            username="ipo-overview-chart-tester",
            password="test-password",
        )
        self.member.user = user
        self.member.save(update_fields=["user"])
        selected_trade = self.make_trade(
            allotted_lots=1,
            sold_lots=1,
            sell_price=Decimal("12"),
            sell_date=date(2026, 6, 9),
        )
        historical_listing = HkIpoListing.objects.create(
            stock_code="HIST25.US",
            stock_name="历史亏损股",
            company_name="历史亏损股有限公司",
            subscription_end_date=date(2025, 9, 10),
            allotment_result_date=date(2025, 9, 11),
            listing_date=date(2025, 9, 12),
            final_price=Decimal("10"),
            lot_size=100,
        )
        historical_trade = HkIpoSubscriptionTrade.objects.create(
            listing=historical_listing,
            member=self.member,
            account=self.account,
            application_date=date(2025, 9, 8),
            applied_lots=1,
            allotted_lots=1,
            sold_lots=1,
            sell_price=Decimal("9"),
            sell_date=date(2025, 9, 12),
        )
        self.client.force_login(user)

        response = self.client.get(reverse("ipo:index"), {"year": "2026"})

        self.assertEqual(response.context["year_filter"]["selected_year"], "2026")
        self.assertEqual(response.context["metrics"]["listing_total"], 1)
        self.assertEqual(response.context["metrics"]["trade_applied"], 1)
        self.assertEqual(response.context["metrics"]["trade_allotted"], 1)
        self.assertEqual(response.context["metrics"]["trade_closed"], 1)
        self.assertEqual(
            response.context["metrics"]["realized_profit_total"],
            selected_trade.realized_profit,
        )
        self.assertEqual(len(response.context["chart_data"]["stock"]), 1)
        self.assertEqual(response.context["chart_data"]["trend"]["labels"], [
            "1月",
            "2月",
            "3月",
            "4月",
            "5月",
            "6月",
            "7月",
            "8月",
            "9月",
            "10月",
            "11月",
            "12月",
        ])
        self.assertEqual(
            response.context["chart_data"]["trend"]["values"][5],
            float(selected_trade.realized_profit),
        )
        self.assertNotContains(response, "中签比例")
        self.assertContains(response, "ipo-stock-profit-chart")
        self.assertContains(response, "ipo-account-profit-chart")
        self.assertContains(response, "ipo-profit-trend-chart")

        self.client.get(reverse("ipo:subscription_trade_list"), {"year": "2025"})
        persisted_response = self.client.get(reverse("ipo:index"))
        self.assertEqual(
            persisted_response.context["year_filter"]["selected_year"],
            "2026",
        )

        all_years_response = self.client.get(reverse("ipo:index"), {"year": "all"})

        self.assertEqual(
            all_years_response.context["chart_data"]["trend"]["labels"],
            ["2025年", "2026年"],
        )
        self.assertEqual(
            all_years_response.context["chart_data"]["trend"]["values"],
            [
                float(historical_trade.realized_profit),
                float(selected_trade.realized_profit),
            ],
        )


class IpoImageRecognitionApiKeyTests(TestCase):
    def test_get_api_key_uses_provider_configured_environment_variable(self):
        provider = AiProvider.objects.create(
            name="BigModel",
            provider_type="openai_compatible",
            extra_data={"api_key_env_var": "CUSTOM_BIGMODEL_KEY"},
        )

        with patch.dict(os.environ, {"CUSTOM_BIGMODEL_KEY": "env-secret"}, clear=True):
            self.assertEqual(get_api_key(provider), "env-secret")

    def test_configured_environment_variable_does_not_fall_back_to_another_provider_key(self):
        provider = AiProvider.objects.create(
            name="豆包视觉",
            provider_type="vision",
            extra_data={"api_key_env_var": "ARK_API_KEY"},
        )

        with patch.dict(os.environ, {"ZHIPU_API_KEY": "zhipu-secret"}, clear=True):
            with self.assertRaisesMessage(IpoImageRecognitionError, "ARK_API_KEY"):
                get_api_key(provider)

    def test_get_api_key_rejects_database_api_key(self):
        provider = AiProvider.objects.create(
            name="BigModel",
            provider_type="openai_compatible",
            extra_data={"api_key": "db-secret"},
        )

        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesMessage(IpoImageRecognitionError, "不能保存在数据库"):
                get_api_key(provider)

    def test_http_authentication_error_is_not_retried_or_masked_by_timeout(self):
        AiProvider.objects.create(
            name="BigModel",
            provider_type="openai_compatible",
            base_url="https://open.bigmodel.cn/api/paas/v4",
            model_name="glm-5v-turbo",
        )
        upload = SimpleUploadedFile("ipo.png", b"image", content_type="image/png")
        error = urllib.error.HTTPError(
            "https://open.bigmodel.cn/api/paas/v4/chat/completions",
            401,
            "Unauthorized",
            {},
            io.BytesIO(b"invalid key"),
        )

        with (
            patch.dict(os.environ, {"ZHIPU_API_KEY": "valid-looking-key"}, clear=True),
            patch("ipo.services.urllib.request.urlopen", side_effect=error) as urlopen,
        ):
            with self.assertRaisesMessage(IpoImageRecognitionError, "HTTP 401"):
                recognize_ipo_listing_from_image(upload)

        self.assertEqual(urlopen.call_count, 1)

    def test_explicit_provider_selection_is_used_for_recognition(self):
        zhipu = AiProvider.objects.create(
            name="智谱视觉",
            provider_type="vision",
            base_url="https://open.bigmodel.cn/api/paas/v4",
            model_name="glm-5v-turbo",
        )
        doubao = AiProvider.objects.create(
            name="豆包视觉",
            provider_type="vision",
            base_url="https://ark.cn-beijing.volces.com/api/v3",
            model_name="doubao-seed-2-0-lite-260215",
            extra_data={"api_key_env_var": "ARK_API_KEY", "image_detail": "high"},
        )

        self.assertEqual(get_active_vision_provider(zhipu.pk), zhipu)
        self.assertEqual(get_active_vision_provider(doubao.pk), doubao)

    def test_disabled_provider_cannot_be_selected(self):
        provider = AiProvider.objects.create(
            name="已停用视觉服务",
            provider_type="vision",
            model_name="vision-model",
            is_active=False,
        )

        with self.assertRaisesMessage(IpoImageRecognitionError, "不可用"):
            get_active_vision_provider(provider.pk)

    def test_doubao_request_uses_selected_model_and_high_detail_image(self):
        provider = AiProvider.objects.create(
            name="豆包视觉",
            provider_type="vision",
            base_url="https://ark.cn-beijing.volces.com/api/v3",
            model_name="doubao-seed-2-0-lite-260215",
            extra_data={"api_key_env_var": "ARK_API_KEY", "image_detail": "high"},
        )
        upload = SimpleUploadedFile("ipo.png", b"image", content_type="image/png")
        response_body = io.BytesIO(
            json.dumps(
                {"choices": [{"message": {"content": '{"stock_code":"09999.HK"}'}}]}
            ).encode("utf-8")
        )

        with (
            patch.dict(os.environ, {"ARK_API_KEY": "ark-test-key"}, clear=True),
            patch("ipo.services.urllib.request.urlopen", return_value=response_body) as urlopen,
        ):
            fields = recognize_ipo_listing_from_image(upload, provider_id=provider.pk)

        request = urlopen.call_args.args[0]
        payload = json.loads(request.data)
        image_url = payload["messages"][0]["content"][1]["image_url"]
        self.assertEqual(request.full_url, "https://ark.cn-beijing.volces.com/api/v3/chat/completions")
        self.assertEqual(payload["model"], "doubao-seed-2-0-lite-260215")
        self.assertEqual(image_url["detail"], "high")
        self.assertTrue(image_url["url"].startswith("data:image/png;base64,"))
        self.assertEqual(fields["stock_code"], "09999.HK")

    def test_doubao_request_uses_ipv4_doh_fallback_after_connection_reset(self):
        provider = AiProvider.objects.create(
            name="豆包视觉",
            provider_type="vision",
            base_url="https://ark.cn-beijing.volces.com/api/v3",
            model_name="doubao-seed-2-0-lite-260215",
            extra_data={"api_key_env_var": "ARK_API_KEY"},
        )
        upload = SimpleUploadedFile("ipo.png", b"image", content_type="image/png")
        doh_payload = {
            "Status": 0,
            "Answer": [
                {
                    "name": "ark.cn-beijing.volces.com.",
                    "type": 1,
                    "TTL": 60,
                    "data": "14.103.169.114",
                }
            ],
        }
        response_body = json.dumps(
            {"choices": [{"message": {"content": '{"stock_code":"09999.HK"}'}}]}
        ).encode()

        with (
            patch.dict(os.environ, {"ARK_API_KEY": "ark-test-key"}, clear=True),
            patch(
                "ipo.services.urllib.request.urlopen",
                side_effect=[
                    urllib.error.URLError(ConnectionResetError(104, "reset")),
                    io.BytesIO(json.dumps(doh_payload).encode()),
                ],
            ),
            patch(
                "ipo.services._read_https_via_ipv4",
                return_value=response_body,
            ) as direct_ipv4,
        ):
            fields = recognize_ipo_listing_from_image(upload, provider_id=provider.pk)

        direct_ipv4.assert_called_once()
        self.assertEqual(direct_ipv4.call_args.args[1], "14.103.169.114")
        self.assertEqual(fields["stock_code"], "09999.HK")


class IpoUploadValidationTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="upload-tester",
            password="password",
        )
        family = Family.objects.create(name="上传测试家庭")
        FamilyMember.objects.create(
            family=family,
            user=self.user,
            display_name="上传测试成员",
        )
        self.client.force_login(self.user)

    def test_image_recognition_rejects_unsupported_image_type(self):
        upload = SimpleUploadedFile("image.gif", b"GIF89a", content_type="image/gif")

        response = self.client.post(
            reverse("ipo:recognize_listing_image"),
            {"image": upload},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("JPG", response.json()["error"])

    def test_image_recognition_rejects_file_larger_than_eight_megabytes(self):
        upload = SimpleUploadedFile(
            "large.jpg",
            b"x" * (8 * 1024 * 1024 + 1),
            content_type="image/jpeg",
        )

        response = self.client.post(
            reverse("ipo:recognize_listing_image"),
            {"image": upload},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("8 MB", response.json()["error"])

    def test_listing_form_uploads_original_image_without_lossy_optimization(self):
        response = self.client.get(reverse("ipo:listing_create"))

        self.assertContains(response, 'formData.append("image", file)')
        self.assertContains(response, 'formData.append("provider", providerSelect.value)')
        self.assertNotContains(response, "createImageBitmap")
        self.assertNotContains(response, "canvas.toBlob")

    def test_prospectus_accepts_pdf_and_rejects_other_file_types(self):
        form = HkIpoListingForm()
        pdf = SimpleUploadedFile("prospectus.pdf", b"%PDF-1.7", content_type="application/pdf")
        form.cleaned_data = {"prospectus": pdf}
        self.assertIs(form.clean_prospectus(), pdf)

        image = SimpleUploadedFile("prospectus.png", b"png", content_type="image/png")
        form.cleaned_data = {"prospectus": image}
        with self.assertRaisesMessage(ValidationError, "PDF"):
            form.clean_prospectus()


class HkIpoListingOptionTests(TestCase):
    def test_active_admin_option_appears_in_listing_form_and_display(self):
        HkIpoListingOption.objects.create(
            category=HkIpoListingOption.CATEGORY_LISTING_TYPE,
            code="secondary",
            name="介绍上市",
            sort_order=60,
        )

        form = HkIpoListingForm()
        listing = HkIpoListing(listing_type="secondary")

        self.assertIn(
            ("secondary", "介绍上市"),
            list(form.fields["listing_type"].choices),
        )
        self.assertEqual(listing.get_listing_type_display(), "介绍上市")

    def test_inactive_option_is_hidden_but_preserved_when_editing_existing_listing(self):
        option = HkIpoListingOption.objects.create(
            category=HkIpoListingOption.CATEGORY_MECHANISM,
            code="custom",
            name="自定义机制",
            is_active=False,
        )
        listing = HkIpoListing(mechanism=option.code)

        create_form = HkIpoListingForm()
        edit_form = HkIpoListingForm(instance=listing)

        self.assertNotIn(
            ("custom", "自定义机制"),
            list(create_form.fields["mechanism"].choices),
        )
        self.assertIn(
            ("custom", "自定义机制"),
            list(edit_form.fields["mechanism"].choices),
        )

    def test_image_recognition_uses_configured_option(self):
        HkIpoListingOption.objects.create(
            category=HkIpoListingOption.CATEGORY_LISTING_TYPE,
            code="secondary",
            name="介绍上市",
        )

        self.assertEqual(normalize_value("listing_type", "介绍上市"), "secondary")
        self.assertIn("secondary（介绍上市）", build_prompt())


class HkConnectExpectationTests(TestCase):
    def make_listing(self, **kwargs):
        defaults = {
            "stock_code": "09998.HK",
            "company_name": "港股通测试",
            "listing_type": HkIpoListing.TYPE_NEW_LISTING,
            "listing_date": date(2026, 1, 31),
            "h_share_market_cap_100m": Decimal("100"),
            "hk_connect_threshold_100m": Decimal("100"),
        }
        defaults.update(kwargs)
        return HkIpoListing(**defaults)

    def test_new_listing_expectation_rules(self):
        self.assertEqual(
            self.make_listing(h_share_market_cap_100m=Decimal("120")).hk_connect_expectation,
            "入通",
        )
        self.assertEqual(
            self.make_listing(h_share_market_cap_100m=Decimal("110")).hk_connect_expectation,
            "入通（10.00%）",
        )
        self.assertEqual(
            self.make_listing(h_share_market_cap_100m=Decimal("80")).hk_connect_expectation,
            "入通涨幅 25.00%",
        )

    def test_ah_expectation_uses_greenshoe_date_rule(self):
        without_greenshoe = self.make_listing(
            listing_type=HkIpoListing.TYPE_AH,
            has_greenshoe=False,
        )
        with_greenshoe = self.make_listing(
            listing_type=HkIpoListing.TYPE_AH,
            has_greenshoe=True,
        )

        self.assertEqual(without_greenshoe.hk_connect_expectation, "2026-01-31 入通")
        self.assertEqual(with_greenshoe.hk_connect_expectation, "2026-02-28 入通")

    def test_wvr_expectation_rules(self):
        below_threshold = self.make_listing(
            listing_type=HkIpoListing.TYPE_WVR,
            h_share_market_cap_100m=Decimal("160"),
        )
        at_threshold = self.make_listing(
            listing_type=HkIpoListing.TYPE_WVR,
            h_share_market_cap_100m=Decimal("200"),
        )

        self.assertEqual(below_threshold.hk_connect_expectation, "入通涨幅 25.00%")
        self.assertEqual(at_threshold.hk_connect_expectation, "2026-08-28 入通")

    def test_gem_does_not_enter_hk_connect_and_other_uses_new_listing_rule(self):
        gem = self.make_listing(listing_type=HkIpoListing.TYPE_GEM)
        other = self.make_listing(
            listing_type=HkIpoListing.TYPE_OTHER,
            h_share_market_cap_100m=Decimal("80"),
        )

        self.assertEqual(gem.hk_connect_expectation, "不入通")
        self.assertEqual(other.hk_connect_expectation, "入通涨幅 25.00%")

    def test_listing_form_does_not_allow_manual_threshold_entry(self):
        form = HkIpoListingForm()

        self.assertNotIn("hk_connect_threshold_100m", form.fields)

    def test_threshold_fetch_converts_hkd_to_hundred_million_hkd(self):
        payload = {
            "result": 1,
            "data": {
                "checkDate": "2026-06-26",
                "inThreshold": 10224598534.8177,
            },
        }
        _hk_connect_threshold_cache.update(
            {"fetched_at": None, "value": None, "check_date": None}
        )

        with patch(
            "ipo.services.urllib.request.urlopen",
            return_value=io.BytesIO(json.dumps(payload).encode("utf-8")),
        ):
            threshold = fetch_hk_connect_threshold_100m(force=True)

        self.assertEqual(
            threshold.quantize(Decimal("0.0001")),
            Decimal("102.2460"),
        )


class HkIpoExpectedMarginTests(TestCase):
    def setUp(self):
        _vbkr_margin_cache.update({"fetched_at": None, "data": {}})
        self.family = Family.objects.create(name="孖展测试家庭")

    def link_user(self, user):
        FamilyMember.objects.create(
            family=self.family,
            user=user,
            display_name=user.username,
        )

    def test_margin_fetch_uses_doh_ipv4_fallback_when_normal_dns_route_fails(self):
        doh_payload = {
            "Status": 0,
            "Answer": [
                {
                    "name": "www.vbkr.com.",
                    "type": 1,
                    "TTL": 60,
                    "data": "150.109.153.48",
                }
            ],
        }
        html = (
            "02667.HK 同仁堂医养 最大10倍杠杆融资 84.19倍 "
            "01770.HK 东方科脉 最大10倍杠杆融资 50.69倍"
        ).encode()

        with (
            patch(
                "ipo.services.urllib.request.urlopen",
                side_effect=[
                    urllib.error.URLError("direct route unavailable"),
                    io.BytesIO(json.dumps(doh_payload).encode()),
                ],
            ),
            patch(
                "ipo.services._read_https_via_ipv4",
                return_value=html,
            ) as direct_ipv4,
        ):
            data = fetch_vbkr_expected_margin_multiples()

        direct_ipv4.assert_called_once()
        self.assertEqual(direct_ipv4.call_args.args[1], "150.109.153.48")
        self.assertEqual(data["02667.HK"], "84.19倍")
        self.assertEqual(data["01770"], "50.69倍")

    def test_listing_page_shows_expected_margin_for_subscribing_and_waiting_tables(self):
        user = get_user_model().objects.create_user(
            username="listing-metric-tester",
            password="test-password",
        )
        self.link_user(user)
        today = timezone.localdate()
        HkIpoListing.objects.create(
            stock_code="09996.HK",
            company_name="招股中测试",
            subscription_start_date=date(2026, 6, 27),
            subscription_end_date=date(2026, 6, 28),
            listing_date=date(2026, 7, 2),
            final_price=Decimal("11"),
            lot_size=100,
        )
        HkIpoListing.objects.create(
            stock_code="09995.HK",
            company_name="待上市测试",
            subscription_start_date=date(2026, 6, 20),
            subscription_end_date=date(2026, 6, 26),
            listing_date=date(2026, 7, 1),
            final_price=Decimal("11"),
            lot_size=100,
        )
        HkIpoListing.objects.create(
            stock_code="09994.HK",
            company_name="今日暗盘测试",
            subscription_start_date=today - timedelta(days=3),
            subscription_end_date=today - timedelta(days=1),
            allotment_result_date=today,
            listing_date=today + timedelta(days=1),
            final_price=Decimal("11"),
            lot_size=100,
        )
        self.client.force_login(user)

        with (
            patch(
                "ipo.views.get_cached_vbkr_expected_margin_multiples",
                return_value={"09996.HK": "100倍", "09995.HK": "200倍"},
            ),
            patch("ipo.views.refresh_listed_market_data", return_value=0),
        ):
            response = self.client.get(reverse("ipo:listing_list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "预计孖展")
        self.assertContains(response, "港股通预测")
        self.assertContains(response, "后续涨幅")
        self.assertContains(response, "external-link-button", count=3)
        self.assertEqual(response.context["metrics"]["grey_market_today"], 1)
        self.assertEqual(response.context["current_date"], today)
        content = response.content.decode()
        self.assertLess(content.index("当前日期"), content.index("港股通预测"))
        self.assertLess(content.index("港股通预测"), content.index("预计孖展"))
        self.assertLess(content.index("预计孖展"), content.index("后续涨幅"))
        metric_labels = [
            "今日上市数量",
            "今日暗盘数量",
            "招股中数量",
            "待上市数量",
            "新股数量",
        ]
        self.assertEqual(
            [content.index(label) for label in metric_labels],
            sorted(content.index(label) for label in metric_labels),
        )
        self.assertNotContains(response, "甲尾信息")
        self.assertNotContains(response, "乙头信息")
        self.assertNotContains(response, "预测中签情况")

    def test_expected_margin_endpoint_fetches_data_without_blocking_listing_page(self):
        user = get_user_model().objects.create_user(
            username="margin-endpoint-tester",
            password="test-password",
        )
        self.link_user(user)
        self.client.force_login(user)

        with patch(
            "ipo.views.fetch_vbkr_expected_margin_multiples",
            return_value={"02667.HK": "81.31倍"},
        ):
            response = self.client.get(reverse("ipo:expected_margin_data"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["02667.HK"], "81.31倍")

    def test_listing_page_year_filter_limits_metrics_and_tables(self):
        user = get_user_model().objects.create_user(
            username="listing-year-filter-tester",
            password="test-password",
        )
        self.link_user(user)
        HkIpoListing.objects.create(
            stock_code="YEAR25.US",
            stock_name="二零二五新股",
            company_name="二零二五新股有限公司",
            subscription_end_date=date(2025, 9, 10),
            listing_date=date(2025, 9, 12),
            final_price=Decimal("10"),
            lot_size=100,
        )
        HkIpoListing.objects.create(
            stock_code="YEAR26.US",
            stock_name="二零二六新股",
            company_name="二零二六新股有限公司",
            subscription_end_date=date(2026, 9, 10),
            listing_date=date(2026, 9, 12),
            final_price=Decimal("10"),
            lot_size=100,
        )
        self.client.force_login(user)

        with (
            patch(
                "ipo.views.get_cached_vbkr_expected_margin_multiples",
                return_value={},
            ),
            patch("ipo.views.refresh_listed_market_data", return_value=0),
        ):
            response = self.client.get(
                reverse("ipo:listing_list"),
                {"year": "2025"},
            )
            self.client.get(reverse("ipo:index"), {"year": "2026"})
            persisted_response = self.client.get(reverse("ipo:listing_list"))

        self.assertEqual(response.context["year_filter"]["selected_year"], "2025")
        self.assertEqual(response.context["metrics"]["total"], 1)
        self.assertContains(response, "二零二五新股")
        self.assertNotContains(response, "二零二六新股")
        self.assertEqual(
            persisted_response.context["year_filter"]["selected_year"],
            "2025",
        )


class HkIpoListedMarketDataTests(TestCase):
    def test_livermore_fetch_parses_year_range_and_market_fields(self):
        fields = [
            "stock_code",
            "issue_date",
            "industry",
            "over_subscribed_multiple",
            "offering_price",
            "px_open_rate",
            "px_close_rate",
            "inception_px_change_rate",
        ]
        payload = {
            "status": "200",
            "data": {
                "fields": fields,
                "list": [
                    [
                        "06915",
                        "2026-06-30",
                        "生物科技-制药",
                        475.56,
                        11.2,
                        -33.9286,
                        -12.68,
                        8.88,
                    ]
                ],
            },
        }
        with patch(
            "ipo.services.urllib.request.urlopen",
            return_value=io.BytesIO(json.dumps(payload).encode("utf-8")),
        ) as urlopen:
            metrics = fetch_jesselivermore_ipo_metrics((2025, 2026))

        request = urlopen.call_args.args[0]
        self.assertIn("issue_year=2025%2C2026", request.full_url)
        self.assertEqual(metrics["06915"]["listing_date"], date(2026, 6, 30))
        self.assertEqual(metrics["06915"]["industry"], "生物科技-制药")
        self.assertEqual(
            metrics["06915"]["cumulative_change_pct"],
            Decimal("8.88"),
        )

    def test_static_metrics_are_saved_once_and_cumulative_change_is_refreshed(self):
        listing = HkIpoListing.objects.create(
            stock_code="06915.HK",
            stock_name="江西生物",
            company_name="江西生物有限公司",
            subscription_end_date=date(2026, 6, 25),
            listing_date=date(2026, 6, 30),
            final_price=Decimal("10"),
            lot_size=100,
        )
        first_payload = {
            "06915": {
                "listing_date": date(2026, 6, 30),
                "industry": "生物科技-制药",
                "over_subscription_multiple": Decimal("475.56"),
                "final_price": Decimal("11.20"),
                "first_day_open_change_pct": Decimal("-33.9286"),
                "first_day_close_change_pct": Decimal("-12.68"),
                "cumulative_change_pct": Decimal("-12.679"),
            }
        }
        with patch(
            "ipo.services.fetch_jesselivermore_ipo_metrics",
            return_value=first_payload,
        ):
            refresh_listed_market_data([listing], 2026)

        listing.refresh_from_db()
        self.assertEqual(listing.industry, "生物科技-制药")
        self.assertEqual(
            listing.over_subscription_multiple,
            Decimal("475.5600"),
        )
        self.assertEqual(listing.final_price, Decimal("11.2000"))
        self.assertEqual(
            listing.first_day_open_change_pct,
            Decimal("-33.9286"),
        )
        self.assertEqual(
            listing.first_day_close_change_pct,
            Decimal("-12.6800"),
        )
        self.assertEqual(
            listing.cumulative_change_pct,
            Decimal("-12.6790"),
        )
        self.assertIsNotNone(listing.market_data_fetched_at)

        second_payload = {
            "06915": {
                **first_payload["06915"],
                "industry": "不应覆盖的行业",
                "final_price": Decimal("99"),
                "cumulative_change_pct": Decimal("8.88"),
            }
        }
        with patch(
            "ipo.services.fetch_jesselivermore_ipo_metrics",
            return_value=second_payload,
        ):
            refresh_listed_market_data([listing], 2026)

        listing.refresh_from_db()
        self.assertEqual(listing.industry, "生物科技-制药")
        self.assertEqual(listing.final_price, Decimal("11.2000"))
        self.assertEqual(listing.cumulative_change_pct, Decimal("8.8800"))
