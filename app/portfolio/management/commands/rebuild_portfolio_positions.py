from django.core.management.base import BaseCommand

from portfolio.models import InvestmentTransaction
from portfolio.services import rebuild_position


class Command(BaseCommand):
    help = "根据已成交交易流水重建最新持仓及盈亏。"

    def handle(self, *args, **options):
        pairs = (
            InvestmentTransaction.objects.exclude(security=None)
            .values_list("account_id", "security_id")
            .distinct()
        )
        count = 0
        for account_id, security_id in pairs:
            transaction = (
                InvestmentTransaction.objects.select_related("account", "security")
                .filter(account_id=account_id, security_id=security_id)
                .first()
            )
            rebuild_position(transaction.account, transaction.security)
            count += 1
        self.stdout.write(self.style.SUCCESS(f"已重建 {count} 组持仓。"))
