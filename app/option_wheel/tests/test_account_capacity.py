from datetime import date, datetime, timezone as dt_timezone
from decimal import Decimal
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from family_core.models import Family, FamilyMember
from ledger.models import BankAccount
from portfolio.models import (
    CashMovementTypeChoices,
    InvestmentAccount,
    InvestmentCashMovement,
    OptionContract,
    Security,
)

from option_wheel.account_capacity import (
    CapacityImportError,
    build_portfolio_capacity,
    import_portfolio_capacity,
)
from option_wheel.models import WheelBrokerAccountSnapshot


AS_OF = datetime(2026, 8, 31, 10, 0, tzinfo=dt_timezone.utc)


class PortfolioCapacityImportTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.family = Family.objects.create(name="Capacity Family")
        cls.member = FamilyMember.objects.create(
            family=cls.family,
            display_name="Capacity Member",
        )
        cls.account = cls.make_account("盈透证券")
        cls.other = cls.make_account("其他证券")
        cls.tsla = Security.objects.create(
            symbol="TSLA",
            name="Tesla",
            market="US",
            asset_type=Security.TYPE_STOCK,
            currency="USD",
        )
        option_security = Security.objects.create(
            symbol="TSLA260904P00300000",
            name="TSLA Put",
            market="US",
            asset_type=Security.TYPE_OPTION,
            currency="USD",
        )
        cls.put = OptionContract.objects.create(
            security=option_security,
            underlying=cls.tsla,
            option_type=OptionContract.PUT,
            strike_price=Decimal("300"),
            expiration_date=date(2026, 9, 4),
            multiplier=100,
        )

    @classmethod
    def make_account(cls, name):
        bank = BankAccount.objects.create(
            family=cls.family,
            member=cls.member,
            account_name=name,
            supports_investment=True,
        )
        return InvestmentAccount.objects.create(bank_account=bank)

    def valuation(self, *, complete=True, positions=None):
        positions = positions if positions is not None else [
            SimpleNamespace(
                security=self.put.security,
                quantity=Decimal("-1"),
                market_value=Decimal("-300"),
            )
        ]
        return {
            "complete": complete,
            "cash_lines": [
                {
                    "account_id": self.account.pk,
                    "currency": "USD",
                    "amount": Decimal("100000"),
                }
            ],
            "total_asset": Decimal("150000"),
            "positions": positions,
            "missing_prices": [] if complete else ["TSLA"],
            "stale_prices": [],
            "missing_rates": [],
            "errors": [],
        }

    def build(self, valuation=None, **overrides):
        values = {
            "account_id": self.account.pk,
            "confirm_no_margin": True,
            "confirm_no_open_orders": True,
            "source_as_of": AS_OF,
        }
        values.update(overrides)
        with patch(
            "option_wheel.account_capacity.value_historical_portfolio",
            return_value=valuation or self.valuation(),
        ):
            return build_portfolio_capacity(**values)

    def test_builds_cash_secured_capacity_from_portfolio(self):
        evidence = self.build()

        self.assertEqual(evidence.settled_cash, Decimal("100000.0000"))
        self.assertEqual(evidence.nav, Decimal("150000.0000"))
        self.assertEqual(evidence.reserved_cash, Decimal("30000.0000"))
        self.assertEqual(evidence.positions_summary["count"], 1)
        self.assertEqual(
            evidence.open_obligations["items"][0]["kind"],
            "cash_secured_put",
        )

    def test_positive_future_settlement_is_not_available_cash(self):
        InvestmentCashMovement.objects.create(
            account=self.account,
            movement_date=date(2026, 8, 31),
            settlement_date=date(2026, 9, 1),
            movement_type=CashMovementTypeChoices.SELL,
            currency="USD",
            amount=Decimal("5000"),
        )

        evidence = self.build()

        self.assertEqual(evidence.settled_cash, Decimal("95000.0000"))
        self.assertEqual(evidence.unsettled_cash, Decimal("5000.0000"))

    def test_fails_closed_without_confirmations_or_complete_valuation(self):
        with self.assertRaises(CapacityImportError):
            self.build(confirm_no_margin=False)
        with self.assertRaises(CapacityImportError):
            self.build(confirm_no_open_orders=False)
        with self.assertRaises(CapacityImportError):
            self.build(valuation=self.valuation(complete=False))
        with self.assertRaises(CapacityImportError):
            self.build(account_id=self.other.pk)

    def test_covered_calls_use_aggregate_share_coverage(self):
        calls = []
        for strike in (Decimal("350"), Decimal("360")):
            security = Security.objects.create(
                symbol=f"TSLA-CALL-{strike}",
                name="TSLA Call",
                market="US",
                asset_type=Security.TYPE_OPTION,
                currency="USD",
            )
            OptionContract.objects.create(
                security=security,
                underlying=self.tsla,
                option_type=OptionContract.CALL,
                strike_price=strike,
                expiration_date=date(2026, 9, 4),
                multiplier=100,
            )
            calls.append(security)
        stock = SimpleNamespace(
            security=self.tsla,
            quantity=Decimal("100"),
            market_value=Decimal("35000"),
        )
        short_calls = [
            SimpleNamespace(
                security=security,
                quantity=Decimal("-1"),
                market_value=Decimal("-100"),
            )
            for security in calls
        ]

        one_call = self.build(
            valuation=self.valuation(positions=[stock, short_calls[0]])
        )
        self.assertEqual(
            one_call.open_obligations["items"][0]["kind"], "covered_call"
        )
        with self.assertRaises(CapacityImportError):
            self.build(
                valuation=self.valuation(positions=[stock, *short_calls])
            )

    def test_import_is_dry_run_by_default_and_idempotent_on_commit(self):
        evidence = self.build()

        preview = import_portfolio_capacity(evidence=evidence)
        created = import_portfolio_capacity(evidence=evidence, commit=True)
        duplicate = import_portfolio_capacity(evidence=evidence, commit=True)

        self.assertIsNone(preview.snapshot_id)
        self.assertTrue(created.snapshot_created)
        self.assertFalse(duplicate.snapshot_created)
        self.assertEqual(created.snapshot_id, duplicate.snapshot_id)
        snapshot = WheelBrokerAccountSnapshot.objects.get()
        self.assertEqual(
            snapshot.source_kind,
            WheelBrokerAccountSnapshot.SOURCE_PORTFOLIO_READONLY,
        )

    def test_command_defaults_to_dry_run(self):
        output = StringIO()
        with patch(
            "option_wheel.account_capacity.value_historical_portfolio",
            return_value=self.valuation(),
        ):
            call_command(
                "import_wheel_portfolio_capacity",
                account_id=self.account.pk,
                confirm_no_margin=True,
                confirm_no_open_orders=True,
                stdout=output,
            )

        self.assertIn("mode=DRY-RUN", output.getvalue())
        self.assertFalse(WheelBrokerAccountSnapshot.objects.exists())

    def test_command_requires_explicit_safety_confirmations(self):
        with self.assertRaises(CommandError):
            call_command(
                "import_wheel_portfolio_capacity",
                account_id=self.account.pk,
            )
