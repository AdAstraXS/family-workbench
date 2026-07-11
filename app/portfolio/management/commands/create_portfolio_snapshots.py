from django.core.management.base import BaseCommand
from django.utils import timezone

from family_core.household import get_household_family, get_site_setting
from family_core.models import FamilyMember
from portfolio.models import InvestmentAccount
from portfolio.snapshot_service import create_portfolio_snapshot


class Command(BaseCommand):
    help = "Create idempotent family and member portfolio snapshots from cached quotes."

    def handle(self, *args, **options):
        family = get_household_family()
        if not family:
            self.stderr.write(self.style.WARNING("No household is configured."))
            return
        snapshot_date = timezone.localdate()
        currency = get_site_setting().base_currency
        accounts = InvestmentAccount.objects.filter(
            bank_account__family=family,
            bank_account__is_active=True,
            bank_account__supports_investment=True,
        ).select_related("bank_account")
        create_portfolio_snapshot(family, accounts, snapshot_date, currency)
        count = 1
        for member in FamilyMember.objects.filter(family=family, is_active=True):
            create_portfolio_snapshot(
                family,
                accounts.filter(bank_account__member=member),
                snapshot_date,
                currency,
                member=member,
            )
            count += 1
        self.stdout.write(self.style.SUCCESS(f"Created or updated {count} snapshots."))
