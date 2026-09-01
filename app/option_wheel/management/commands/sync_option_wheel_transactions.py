from django.core.management.base import BaseCommand, CommandError

from family_core.models import Family
from portfolio.models import InvestmentAccount

from option_wheel.lifecycle_service import (
    WheelLifecycleError, eligible_unlinked_transactions, sync_transactions,
)


class Command(BaseCommand):
    help = "把投资组合已成交期权流水幂等关联为车轮周期；默认只预演。"

    def add_arguments(self, parser):
        parser.add_argument("--family-id", type=int, required=True)
        parser.add_argument("--account-ids", nargs="+", type=int, required=True)
        parser.add_argument("--commit", action="store_true")

    def handle(self, *args, **options):
        try:
            family = Family.objects.get(pk=options["family_id"])
        except Family.DoesNotExist:
            raise CommandError("家庭不存在。") from None
        account_ids = list(dict.fromkeys(options["account_ids"]))
        if InvestmentAccount.objects.filter(pk__in=account_ids, bank_account__family=family).count() != len(account_ids):
            raise CommandError("账户不完整或不属于目标家庭。")
        pending = list(eligible_unlinked_transactions(family=family, account_ids=account_ids))
        self.stdout.write(f"发现 {len(pending)} 笔尚未关联的已成交期权卖方流水。")
        if not options["commit"]:
            self.stdout.write(self.style.WARNING("预演完成：未写数据库。"))
            return
        try:
            results = sync_transactions(family=family, account_ids=account_ids)
        except WheelLifecycleError as exc:
            raise CommandError(str(exc)) from None
        self.stdout.write(self.style.SUCCESS(f"已关联 {sum(created for _, created in results)} 笔流水；未修改投资组合。"))
