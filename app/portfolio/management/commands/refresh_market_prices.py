from django.core.management.base import BaseCommand

from portfolio.market_data import refresh_market_data


class Command(BaseCommand):
    help = "批量刷新当前非零持仓行情；可选同时刷新家庭自选标的。"

    def add_arguments(self, parser):
        parser.add_argument(
            "--include-watchlist",
            action="store_true",
            help="除非零持仓外，同时刷新自选标的。",
        )
        parser.add_argument(
            "--security-id",
            action="append",
            type=int,
            dest="security_ids",
            help="仅刷新指定证券 ID，可重复传入。",
        )

    def handle(self, *args, **options):
        run = refresh_market_data(
            include_watchlist=options["include_watchlist"],
            security_ids=options.get("security_ids"),
        )
        message = (
            f"批次 #{run.pk} 状态={run.get_status_display()} 目标={run.target_count} "
            f"成功={run.success_count} 过期={run.stale_count} "
            f"缺失={run.missing_count} 错误={run.error_count}"
        )
        if run.status == "success":
            self.stdout.write(self.style.SUCCESS(message))
        elif run.status == "partial":
            self.stdout.write(self.style.WARNING(message))
        else:
            self.stdout.write(self.style.ERROR(message))
        for error in (run.details or {}).get("errors", [])[:20]:
            self.stdout.write(self.style.ERROR(error))
