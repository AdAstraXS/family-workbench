from datetime import date
from decimal import Decimal

from django.test import TestCase

from family_core.models import Family, FamilyMember
from ledger.models import BankAccount

from .models import HkIpoListing, HkIpoSubscriptionTrade


class HkIpoSubscriptionTradeCalculationTests(TestCase):
    def setUp(self):
        self.family = Family.objects.create(name="Test Family")
        self.member = FamilyMember.objects.create(family=self.family, display_name="Tester")
        self.account = BankAccount.objects.create(
            family=self.family,
            member=self.member,
            account_name="IPO Account",
            remark="打新账户",
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
            "financing_amount": Decimal("10000"),
            "financing_rate": Decimal("7.3"),
            "financing_days": 5,
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

        expected_interest = Decimal("10000") * Decimal("7.3") / Decimal("100") / Decimal("365") * Decimal("5")
        self.assertEqual(trade.allotted_value, Decimal("2000"))
        self.assertEqual(trade.allotment_fee, Decimal("20.00"))
        self.assertEqual(trade.trading_fee, Decimal("10"))
        self.assertEqual(trade.financing_interest, expected_interest)
        self.assertEqual(trade.trade_status, HkIpoSubscriptionTrade.STATUS_CLOSED)
        self.assertEqual(
            trade.realized_profit.quantize(Decimal("0.0001")),
            (Decimal("400") - Decimal("100") - expected_interest - Decimal("20") - Decimal("10")).quantize(Decimal("0.0001")),
        )

    def test_status_changes_from_applying_to_holding_to_closed(self):
        applying = self.make_trade(allotted_lots=None)
        holding = self.make_trade(allotted_lots=2, sold_lots=1, sell_date=date(2026, 6, 9))
        closed = self.make_trade(allotted_lots=2, sold_lots=2, sell_date=date(2026, 6, 9))

        self.assertEqual(applying.trade_status, HkIpoSubscriptionTrade.STATUS_APPLYING)
        self.assertEqual(holding.trade_status, HkIpoSubscriptionTrade.STATUS_HOLDING)
        self.assertEqual(closed.trade_status, HkIpoSubscriptionTrade.STATUS_CLOSED)

    def test_zero_allotted_lots_auto_sets_sell_date_to_allotment_result_date(self):
        trade = self.make_trade(allotted_lots=0, sold_lots=0)

        self.assertEqual(trade.trade_status, HkIpoSubscriptionTrade.STATUS_CLOSED)
        self.assertEqual(trade.sell_date, self.listing.allotment_result_date)
