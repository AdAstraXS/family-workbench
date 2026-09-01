from datetime import date
from decimal import Decimal

from django.test import TestCase

from family_core.models import Family, FamilyMember
from ledger.models import BankAccount
from portfolio.models import (
    InvestmentAccount, InvestmentTransaction, OptionContract, Security,
    TradeStatusChoices, TradeTypeChoices,
)
from option_wheel.lifecycle_service import WheelLifecycleError, sync_transactions
from option_wheel.models import LegStatus, WheelCollateralReservation, WheelCycle, WheelTransactionLink


class WheelLifecycleServiceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.family = Family.objects.create(name="Lifecycle Family")
        member = FamilyMember.objects.create(family=cls.family, display_name="Owner")
        bank = BankAccount.objects.create(
            family=cls.family, member=member, account_name="盈透证券",
            supports_investment=True,
        )
        cls.account = InvestmentAccount.objects.create(bank_account=bank)
        cls.stock = Security.objects.create(
            symbol="TSLA", name="Tesla", market="US",
            asset_type=Security.TYPE_STOCK, currency="USD",
        )
        cls.option_security = Security.objects.create(
            symbol="TSLA260904P00300000", name="TSLA Put", market="US",
            asset_type=Security.TYPE_OPTION, currency="USD",
        )
        cls.contract = OptionContract.objects.create(
            security=cls.option_security, underlying=cls.stock,
            option_type=OptionContract.PUT, strike_price=Decimal("300"),
            expiration_date=date(2026, 9, 4), multiplier=100,
        )

    def create_option_trade(self, **overrides):
        values = {
            "account": self.account, "security": self.option_security,
            "trade_date": date(2026, 8, 31), "trade_type": TradeTypeChoices.SELL,
            "position_effect": InvestmentTransaction.EFFECT_OPEN,
            "status": TradeStatusChoices.COMPLETED, "quantity": Decimal("1"),
            "price": Decimal("3"), "amount": Decimal("300"),
            "fee": Decimal("1"), "tax": Decimal("0"),
            "cash_change": Decimal("299"), "currency": "USD",
        }
        values.update(overrides)
        return InvestmentTransaction.objects.create(**values)

    def test_sell_put_open_is_idempotently_linked_with_full_cash(self):
        self.create_option_trade()
        first = sync_transactions(family=self.family, account_ids=[self.account.pk])
        second = sync_transactions(family=self.family, account_ids=[self.account.pk])

        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        cycle = WheelCycle.objects.get()
        leg = cycle.legs.get()
        reservation = leg.collateral_reservations.get()
        self.assertEqual(leg.premium_total, Decimal("299"))
        self.assertEqual(leg.option_contract, self.contract)
        self.assertEqual(reservation.kind, WheelCollateralReservation.CASH)
        self.assertEqual(reservation.cash_amount, Decimal("30000"))

    def test_put_assignment_releases_cash_and_sets_cost_basis(self):
        self.create_option_trade()
        sync_transactions(family=self.family, account_ids=[self.account.pk])
        stock_trade = InvestmentTransaction.objects.create(
            account=self.account, security=self.stock, trade_date=date(2026, 9, 4),
            trade_type=TradeTypeChoices.BUY, status=TradeStatusChoices.COMPLETED,
            quantity=Decimal("100"), price=Decimal("300"), amount=Decimal("30000"),
            cash_change=Decimal("-30000"), currency="USD",
        )
        self.create_option_trade(
            trade_date=date(2026, 9, 4), trade_type=TradeTypeChoices.BUY,
            position_effect=InvestmentTransaction.EFFECT_CLOSE,
            price=Decimal("0"), amount=Decimal("0"), fee=Decimal("0"),
            cash_change=Decimal("0"),
            extra_data={"option_action": "assignment", "underlying_transaction_id": stock_trade.pk},
        )
        sync_transactions(family=self.family, account_ids=[self.account.pk])

        cycle = WheelCycle.objects.get()
        leg = cycle.legs.get()
        self.assertEqual(cycle.assigned_cost_basis, Decimal("300"))
        self.assertEqual(cycle.assigned_share_quantity, Decimal("100"))
        self.assertEqual(leg.status, LegStatus.ASSIGNED)
        self.assertIsNotNone(leg.collateral_reservations.get().released_at)
        self.assertEqual(WheelTransactionLink.objects.count(), 3)

    def test_partial_close_reduces_only_matching_cash_collateral(self):
        self.create_option_trade(quantity=Decimal("2"), amount=Decimal("600"), cash_change=Decimal("599"))
        sync_transactions(family=self.family, account_ids=[self.account.pk])
        self.create_option_trade(
            trade_type=TradeTypeChoices.BUY,
            position_effect=InvestmentTransaction.EFFECT_CLOSE,
            quantity=Decimal("1"), amount=Decimal("100"),
            cash_change=Decimal("-100"),
        )

        sync_transactions(family=self.family, account_ids=[self.account.pk])

        leg = WheelCycle.objects.get().legs.get()
        reservation = leg.collateral_reservations.get()
        self.assertEqual(leg.status, LegStatus.OPEN)
        self.assertEqual(leg.open_contract_count, 1)
        self.assertEqual(reservation.cash_amount, Decimal("30000.0000"))
        self.assertIsNone(reservation.released_at)
        self.assertEqual(leg.transaction_links.count(), 2)

    def test_one_close_can_be_allocated_across_two_open_legs(self):
        self.create_option_trade()
        sync_transactions(family=self.family, account_ids=[self.account.pk])
        self.create_option_trade()
        sync_transactions(family=self.family, account_ids=[self.account.pk])
        close = self.create_option_trade(
            trade_type=TradeTypeChoices.BUY,
            position_effect=InvestmentTransaction.EFFECT_CLOSE,
            quantity=Decimal("2"), amount=Decimal("200"),
            cash_change=Decimal("-200"),
        )

        sync_transactions(family=self.family, account_ids=[self.account.pk])

        cycle = WheelCycle.objects.get()
        self.assertEqual(cycle.legs.filter(status=LegStatus.CLOSED).count(), 2)
        self.assertEqual(close.wheel_links.count(), 2)
        self.assertTrue(all(item.released_at for item in WheelCollateralReservation.objects.all()))

    def test_same_strike_call_close_cannot_match_open_put(self):
        self.create_option_trade()
        sync_transactions(family=self.family, account_ids=[self.account.pk])
        call_security = Security.objects.create(
            symbol="TSLA260904C00300000", name="TSLA Call", market="US",
            asset_type=Security.TYPE_OPTION, currency="USD",
        )
        OptionContract.objects.create(
            security=call_security, underlying=self.stock,
            option_type=OptionContract.CALL, strike_price=Decimal("300"),
            expiration_date=date(2026, 9, 4), multiplier=100,
        )
        InvestmentTransaction.objects.create(
            account=self.account, security=call_security,
            trade_date=date(2026, 9, 1), trade_type=TradeTypeChoices.BUY,
            position_effect=InvestmentTransaction.EFFECT_CLOSE,
            status=TradeStatusChoices.COMPLETED, quantity=Decimal("1"),
            price=Decimal("1"), amount=Decimal("100"),
            cash_change=Decimal("-100"), currency="USD",
        )

        with self.assertRaises(WheelLifecycleError):
            sync_transactions(
                family=self.family, account_ids=[self.account.pk]
            )

        leg = WheelCycle.objects.get().legs.get()
        self.assertEqual(leg.status, LegStatus.OPEN)
        self.assertEqual(leg.open_contract_count, 1)

    def test_covered_call_cannot_reserve_more_than_assigned_shares(self):
        self.create_option_trade()
        sync_transactions(family=self.family, account_ids=[self.account.pk])
        stock_trade = InvestmentTransaction.objects.create(
            account=self.account, security=self.stock, trade_date=date(2026, 9, 4),
            trade_type=TradeTypeChoices.BUY, status=TradeStatusChoices.COMPLETED,
            quantity=Decimal("100"), price=Decimal("300"), amount=Decimal("30000"),
            cash_change=Decimal("-30000"), currency="USD",
        )
        self.create_option_trade(
            trade_date=date(2026, 9, 4), trade_type=TradeTypeChoices.BUY,
            position_effect=InvestmentTransaction.EFFECT_CLOSE,
            price=Decimal("0"), amount=Decimal("0"), fee=Decimal("0"), cash_change=Decimal("0"),
            extra_data={"option_action": "assignment", "underlying_transaction_id": stock_trade.pk},
        )
        sync_transactions(family=self.family, account_ids=[self.account.pk])
        call_security = Security.objects.create(
            symbol="TSLA260911C00310000", name="TSLA Call", market="US",
            asset_type=Security.TYPE_OPTION, currency="USD",
        )
        OptionContract.objects.create(
            security=call_security, underlying=self.stock, option_type=OptionContract.CALL,
            strike_price=Decimal("310"), expiration_date=date(2026, 9, 11), multiplier=100,
        )
        InvestmentTransaction.objects.create(
            account=self.account, security=call_security, trade_date=date(2026, 9, 7),
            trade_type=TradeTypeChoices.SELL, position_effect=InvestmentTransaction.EFFECT_OPEN,
            status=TradeStatusChoices.COMPLETED, quantity=Decimal("2"), price=Decimal("2"),
            amount=Decimal("400"), cash_change=Decimal("400"), currency="USD",
        )

        with self.assertRaises(WheelLifecycleError):
            sync_transactions(family=self.family, account_ids=[self.account.pk])
