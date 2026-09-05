from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from family_core.models import AccountType, AssetCategory, Family, FamilyMember
from ledger.models import AssetBalanceEntry, AssetBalanceSnapshot, BankAccount
from portfolio.models import InvestmentAccount, PortfolioSnapshot, PortfolioSnapshotPositionLine

from .read_tools import (
    GlobalAiReadError,
    SCOPE_FAMILY,
    ledger_asset_snapshot,
    portfolio_account_snapshot,
)


class GlobalAiReadToolsTests(TestCase):
    def setUp(self):
        self.family = Family.objects.create(name="虚构家庭", base_currency="CNY")
        self.other_family = Family.objects.create(name="其他家庭", base_currency="CNY")
        alice_user = get_user_model().objects.create_user(username="global-ai-alice")
        bob_user = get_user_model().objects.create_user(username="global-ai-bob")
        self.alice = FamilyMember.objects.create(
            family=self.family, user=alice_user, display_name="Alice", display_order=1
        )
        self.bob = FamilyMember.objects.create(
            family=self.family, user=bob_user, display_name="Bob", display_order=2
        )
        self.account_type = AccountType.objects.create(
            family=self.family, name="券商", code="broker"
        )
        self.asset_category = AssetCategory.objects.create(
            family=self.family, name="金融资产", code="financial"
        )
        self.alice_bank = BankAccount.objects.create(
            family=self.family, member=self.alice, account_name="Alice银行"
        )
        self.alice_broker = BankAccount.objects.create(
            family=self.family,
            member=self.alice,
            account_name="Alice券商",
            account_type_ref=self.account_type,
            supports_investment=True,
            is_active=False,
        )
        self.bob_broker = BankAccount.objects.create(
            family=self.family,
            member=self.bob,
            account_name="Bob券商",
            account_type_ref=self.account_type,
            supports_investment=True,
        )
        self.formal = AssetBalanceSnapshot.objects.create(
            family=self.family,
            snapshot_date=date(2026, 8, 31),
            base_currency="CNY",
            is_draft=False,
        )
        for member, account, amount in (
            (self.alice, self.alice_bank, "200000"),
            (self.alice, self.alice_broker, "800000"),
            (self.bob, self.bob_broker, "400000"),
        ):
            AssetBalanceEntry.objects.create(
                snapshot=self.formal,
                member=member,
                account=account,
                asset_category=self.asset_category,
                currency="CNY",
                original_amount=Decimal(amount),
                base_amount=Decimal(amount),
            )
        draft = AssetBalanceSnapshot.objects.create(
            family=self.family,
            snapshot_date=date(2026, 9, 30),
            base_currency="CNY",
            is_draft=True,
        )
        AssetBalanceEntry.objects.create(
            snapshot=draft,
            member=self.alice,
            account=self.alice_bank,
            currency="CNY",
            original_amount=Decimal("9999999"),
            base_amount=Decimal("9999999"),
        )

        self.alice_investment = InvestmentAccount.objects.create(
            bank_account=self.alice_broker
        )
        self.bob_investment = InvestmentAccount.objects.create(bank_account=self.bob_broker)
        self.portfolio_snapshot = PortfolioSnapshot.objects.create(
            family=self.family,
            member=self.alice,
            account=self.alice_investment,
            snapshot_date=date(2026, 9, 1),
            total_cash=Decimal("100000"),
            total_market_value=Decimal("700000"),
            total_asset=Decimal("800000"),
            currency="CNY",
            extra_data={"complete": True},
        )
        for asset_type, name, amount in (
            ("cash", "CNY现金", "100000"),
            ("stock", "虚构股票", "500000"),
            ("bond", "虚构债券", "200000"),
        ):
            PortfolioSnapshotPositionLine.objects.create(
                snapshot=self.portfolio_snapshot,
                account=self.alice_investment,
                asset_type=asset_type,
                asset_name=name,
                quantity=Decimal("1"),
                price=Decimal(amount),
                price_as_of=date(2026, 9, 1),
                price_source="synthetic",
                pricing_status="fresh",
                currency="CNY",
                fx_rate=Decimal("1"),
                fx_rate_as_of=date(2026, 9, 1),
                market_value_original=Decimal(amount),
                market_value=Decimal(amount),
            )

    def test_personal_ledger_uses_latest_formal_snapshot_and_keeps_inactive_account(self):
        with patch("ai_analysis.read_tools.PortfolioSnapshot.objects.filter") as portfolio_filter:
            result = ledger_asset_snapshot(self.alice)
        portfolio_filter.assert_not_called()
        self.assertEqual(result["module"], "ledger")
        self.assertEqual(result["snapshot_date"], "2026-08-31")
        self.assertEqual(result["total_base_amount"], "1000000.0000")
        self.assertEqual({row["account_name"] for row in result["accounts"]}, {
            "Alice银行", "Alice券商"
        })

    def test_family_ledger_scope_includes_both_members(self):
        result = ledger_asset_snapshot(self.alice, scope=SCOPE_FAMILY)
        self.assertEqual(result["total_base_amount"], "1400000.0000")
        self.assertEqual({row["member_name"] for row in result["accounts"]}, {"Alice", "Bob"})

    def test_ledger_missing_exchange_rate_does_not_claim_complete_total(self):
        AssetBalanceEntry.objects.create(
            snapshot=self.formal,
            member=self.alice,
            account_name="美元账户",
            currency="USD",
            original_amount=Decimal("1000"),
            base_amount=Decimal("1000"),
        )
        result = ledger_asset_snapshot(self.alice)
        self.assertFalse(result["complete"])
        self.assertIsNone(result["total_base_amount"])
        self.assertEqual(result["missing_exchange_rates"], ["USD"])

    def test_portfolio_account_is_independent_and_preserves_asset_types(self):
        with patch("ai_analysis.read_tools.AssetBalanceSnapshot.objects.filter") as ledger_filter:
            result = portfolio_account_snapshot(
                self.alice, account_id=self.alice_investment.pk
            )
        ledger_filter.assert_not_called()
        self.assertEqual(result["module"], "portfolio")
        self.assertEqual(result["total_asset"], "800000.0000")
        self.assertEqual(result["percentages_by_asset_type"], {
            "bond": "25.00", "cash": "12.50", "stock": "62.50"
        })
        self.assertNotIn("ledger", result)

    def test_personal_scope_cannot_read_other_members_investment_account(self):
        with self.assertRaises(GlobalAiReadError):
            portfolio_account_snapshot(self.alice, account_id=self.bob_investment.pk)

    def test_family_scope_can_read_other_members_investment_account(self):
        bob_snapshot = PortfolioSnapshot.objects.create(
            family=self.family,
            member=self.bob,
            account=self.bob_investment,
            snapshot_date=date(2026, 9, 1),
            total_asset=Decimal("300000"),
            currency="CNY",
            extra_data={"complete": True},
        )
        result = portfolio_account_snapshot(
            self.alice,
            account_id=self.bob_investment.pk,
            scope=SCOPE_FAMILY,
            snapshot_id=bob_snapshot.pk,
        )
        self.assertEqual(result["member_name"], "Bob")

    def test_incomplete_portfolio_snapshot_has_no_percentages(self):
        self.portfolio_snapshot.extra_data = {
            "complete": False,
            "missing_prices": [{"security": "虚构股票"}],
        }
        self.portfolio_snapshot.save(update_fields=["extra_data"])
        result = portfolio_account_snapshot(
            self.alice, account_id=self.alice_investment.pk
        )
        self.assertFalse(result["complete"])
        self.assertIsNone(result["percentages_by_asset_type"])
        self.assertIn("该快照存在缺失价格。", result["warnings"])

    def test_snapshot_and_account_ids_are_scoped_to_actor_family(self):
        outsider = FamilyMember.objects.create(
            family=self.other_family, display_name="Outsider"
        )
        with self.assertRaises(GlobalAiReadError):
            ledger_asset_snapshot(outsider, snapshot_id=self.formal.pk)
        with self.assertRaises(GlobalAiReadError):
            portfolio_account_snapshot(
                outsider, account_id=self.alice_investment.pk, scope=SCOPE_FAMILY
            )
