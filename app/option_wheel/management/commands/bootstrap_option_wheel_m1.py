from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from family_core.models import Family
from portfolio.models import InvestmentAccount, Security

from option_wheel.models import WheelPolicy


class Command(BaseCommand):
    help = "幂等建立 M1 试点策略（TSLA、MSFT、NVDA）；默认只预演。"

    def add_arguments(self, parser):
        parser.add_argument("--family-id", type=int, required=True)
        parser.add_argument("--account-ids", nargs="+", type=int, required=True)
        parser.add_argument("--symbols", nargs="+", default=["TSLA", "MSFT", "NVDA"])
        parser.add_argument("--commit", action="store_true")

    def handle(self, *args, **options):
        try:
            family = Family.objects.get(pk=options["family_id"])
        except Family.DoesNotExist:
            raise CommandError("家庭不存在。") from None
        accounts = list(
            InvestmentAccount.objects.filter(
                pk__in=options["account_ids"], bank_account__family=family,
            ).select_related("bank_account")
        )
        if len(accounts) != len(set(options["account_ids"])):
            raise CommandError("账户不完整或不属于目标家庭。")
        symbols = [value.upper().removeprefix("US.") for value in options["symbols"]]
        securities = list(
            Security.objects.filter(
                symbol__in=symbols, market__iexact="US",
                asset_type=Security.TYPE_STOCK, currency="USD",
            )
        )
        if {item.symbol.upper() for item in securities} != set(symbols):
            raise CommandError("试点标的未完整映射到本地 USD 美股资料。")
        total = len(accounts) * len(securities)
        self.stdout.write(f"将核对 {total} 条账户—标的策略；默认 DTE 4–9 天、权利金 200–400 USD。")
        if not options["commit"]:
            self.stdout.write(self.style.WARNING("预演完成：未写数据库。"))
            return
        created = 0
        with transaction.atomic():
            for account in accounts:
                for security in securities:
                    _, was_created = WheelPolicy.objects.get_or_create(
                        family=family, account=account, underlying=security,
                    )
                    created += int(was_created)
        self.stdout.write(self.style.SUCCESS(f"策略已就绪：新增 {created} 条，已有 {total - created} 条。"))
