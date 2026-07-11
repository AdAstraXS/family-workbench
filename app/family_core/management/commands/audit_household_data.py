from django.core.management.base import BaseCommand
from django.db import connection
from django.db.models import Count, F, Q

from family_core.models import Family, SiteSetting
from ipo.models import HkIpoSubscriptionTrade
from ledger.models import BankAccount, ExpenseRecord, IncomeRecord
from portfolio.models import (
    InvestmentPosition,
    PortfolioSnapshot,
    WatchlistItem,
)


class Command(BaseCommand):
    help = "Read-only checks for the single-household data boundary."

    def handle(self, *args, **options):
        site_setting_table_exists = (
            SiteSetting._meta.db_table in connection.introspection.table_names()
        )
        checks = {
            "families": Family.objects.count(),
            "bank_accounts_member_family_mismatch": BankAccount.objects.exclude(
                family_id=F("member__family_id")
            ).count(),
            "income_member_family_mismatch": IncomeRecord.objects.exclude(
                family_id=F("member__family_id")
            ).count(),
            "income_account_owner_mismatch": IncomeRecord.objects.filter(
                bank_account__isnull=False
            ).exclude(
                Q(bank_account__family_id=F("family_id"))
                & Q(bank_account__member_id=F("member_id"))
            ).count(),
            "expense_member_family_mismatch": ExpenseRecord.objects.exclude(
                family_id=F("member__family_id")
            ).count(),
            "expense_account_owner_mismatch": ExpenseRecord.objects.filter(
                bank_account__isnull=False
            ).exclude(
                Q(bank_account__family_id=F("family_id"))
                & Q(bank_account__member_id=F("member_id"))
            ).count(),
            "watchlist_member_family_mismatch": WatchlistItem.objects.filter(
                member__isnull=False
            ).exclude(family_id=F("member__family_id")).count(),
            "snapshot_member_family_mismatch": PortfolioSnapshot.objects.filter(
                member__isnull=False
            ).exclude(family_id=F("member__family_id")).count(),
            "snapshot_account_family_mismatch": PortfolioSnapshot.objects.filter(
                account__isnull=False
            ).exclude(family_id=F("account__bank_account__family_id")).count(),
            "ipo_account_owner_mismatch": HkIpoSubscriptionTrade.objects.filter(
                account__isnull=False
            ).exclude(account__member_id=F("member_id")).count(),
            "duplicate_position_rows": InvestmentPosition.objects.values(
                "account_id", "security_id"
            ).annotate(row_count=Count("id")).filter(row_count__gt=1).count(),
        }
        if site_setting_table_exists:
            checks["site_settings"] = SiteSetting.objects.count()
        else:
            self.stdout.write("site_settings: not migrated")

        failed = False
        for name, count in checks.items():
            self.stdout.write(f"{name}: {count}")
            if name == "families":
                failed = failed or count != 1
            elif name == "site_settings":
                failed = failed or count != 1
            else:
                failed = failed or count != 0

        if failed:
            self.stderr.write(self.style.WARNING("Household data audit found issues."))
        else:
            self.stdout.write(self.style.SUCCESS("Household data audit passed."))
