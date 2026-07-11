from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from family_core.models import AccountType, Family, FamilyMember
from ledger.models import BankAccount
from portfolio.account_sync import sync_investment_account
from portfolio.models import (
    InvestmentCashMovement,
    InvestmentPosition,
    Security,
)


class DashboardTests(TestCase):
    def test_home_uses_bank_account_as_investment_account_source(self):
        family = Family.objects.create(name="Dashboard Family")
        user = get_user_model().objects.create_user(username="dashboard-user")
        member = FamilyMember.objects.create(
            family=family,
            user=user,
            display_name="Renamed Member",
        )
        account_type = AccountType.objects.create(family=family, name="Brokerage")
        bank_account = BankAccount.objects.create(
            family=family,
            member=member,
            account_name="Broker",
            account_type_ref=account_type,
            supports_investment=True,
        )
        account = sync_investment_account(bank_account)
        security = Security.objects.create(
            symbol="DASH",
            name="Dashboard Security",
            market="HK",
        )
        InvestmentCashMovement.objects.create(
            account=account,
            movement_date=date.today(),
            movement_type="deposit",
            currency="CNY",
            amount=Decimal("100"),
        )
        InvestmentPosition.objects.create(
            account=account,
            security=security,
            position_date=date.today(),
            market_value=Decimal("200"),
        )
        self.client.force_login(user)

        response = self.client.get(reverse("dashboard:home"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["total_investment_asset"], Decimal("300"))
