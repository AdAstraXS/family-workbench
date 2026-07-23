from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django.utils.dateparse import parse_date

from portfolio.daily_valuation import (
    DailyPortfolioValuationError,
    run_daily_portfolio_valuation,
)
from portfolio.models import MarketDataRunStatusChoices


class Command(BaseCommand):
    help = "刷新行情和汇率，检查估值完整性，并生成账户、成员和家庭三级每日快照。"

    def add_arguments(self, parser):
        parser.add_argument(
            "--date",
            help="估值日期 YYYY-MM-DD；默认今天。",
        )
        parser.add_argument(
            "--include-watchlist",
            action="store_true",
            help="除非零持仓外，同时刷新家庭自选标的。",
        )
        parser.add_argument(
            "--require-complete",
            action="store_true",
            help="存在缺价、缺汇率或流水错误时停止，不写入快照。",
        )
        parser.add_argument(
            "--fail-on-warning",
            action="store_true",
            help="部分成功时保留快照和记录，但以非零状态退出，便于定时任务告警。",
        )

    def handle(self, *args, **options):
        valuation_date = timezone.localdate()
        if options.get("date"):
            valuation_date = parse_date(options["date"])
            if not valuation_date:
                raise CommandError(f"无效日期：{options['date']}")
        if valuation_date > timezone.localdate():
            raise CommandError("估值日期不能晚于今天。")

        try:
            run = run_daily_portfolio_valuation(
                valuation_date=valuation_date,
                include_watchlist=options["include_watchlist"],
                require_complete=options["require_complete"],
            )
        except DailyPortfolioValuationError as exc:
            raise CommandError(str(exc)) from exc

        message = (
            f"{valuation_date} 每日投资组合估值 #{run.pk}："
            f"状态={run.get_status_display()}，快照={run.snapshot_count}，"
            f"行情成功={run.quote_success_count}，过期={run.stale_price_count}，"
            f"缺价={run.missing_price_count}，缺汇率={run.missing_exchange_rate_count}，"
            f"错误={run.error_count}"
        )
        if run.status == MarketDataRunStatusChoices.SUCCESS:
            self.stdout.write(self.style.SUCCESS(message))
        else:
            self.stdout.write(self.style.WARNING(message))
            if options["fail_on_warning"]:
                raise CommandError(f"{message}；快照已保留，请检查运行详情。")
