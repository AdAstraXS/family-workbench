from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from family_core.models import Family, FamilyMember
from ledger.models import BankAccount
from portfolio.models import InvestmentAccount, Security
from option_wheel.models import WheelPolicy


class WheelManagementCommandTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.family = Family.objects.create(name="Command Family")
        member = FamilyMember.objects.create(family=cls.family, display_name="Owner")
        bank = BankAccount.objects.create(
            family=cls.family, member=member, account_name="盈透证券",
            supports_investment=True,
        )
        cls.account = InvestmentAccount.objects.create(bank_account=bank)
        for symbol in ("TSLA", "MSFT", "NVDA"):
            Security.objects.create(
                symbol=symbol, name=symbol, market="US",
                asset_type=Security.TYPE_STOCK, currency="USD",
            )

    def test_bootstrap_is_dry_run_then_idempotent(self):
        call_command(
            "bootstrap_option_wheel_m1", family_id=self.family.pk,
            account_ids=[self.account.pk], stdout=StringIO(),
        )
        self.assertEqual(WheelPolicy.objects.count(), 0)
        call_command(
            "bootstrap_option_wheel_m1", family_id=self.family.pk,
            account_ids=[self.account.pk], commit=True, stdout=StringIO(),
        )
        call_command(
            "bootstrap_option_wheel_m1", family_id=self.family.pk,
            account_ids=[self.account.pk], commit=True, stdout=StringIO(),
        )
        self.assertEqual(WheelPolicy.objects.count(), 3)

    @patch("option_wheel.management.commands.refresh_option_wheel_analysis.persist_probe_symbol")
    @patch("option_wheel.management.commands.refresh_option_wheel_analysis.run_probe")
    def test_refresh_requires_commit_before_persisting(self, run_probe, persist):
        run_probe.return_value = {
            "status": "success", "symbols": [{"symbol": "US.TSLA"}],
            "subscription": {"cleanup_status": "restored"},
        }
        call_command(
            "refresh_option_wheel_analysis", family_id=self.family.pk,
            account_id=self.account.pk, symbols=["TSLA"], stdout=StringIO(),
        )
        persist.assert_not_called()
        call_command(
            "refresh_option_wheel_analysis", family_id=self.family.pk,
            account_id=self.account.pk, symbols=["TSLA"], commit=True,
            stdout=StringIO(),
        )
        persist.assert_called_once()
