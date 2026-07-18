from collections import defaultdict
from datetime import datetime, time

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django.utils.dateparse import parse_date

from family_core.household import get_household_family
from portfolio.historical_valuation import account_ids_as_of
from portfolio.management.commands.analyze_portfolio_snapshot_gaps import (
    DEFAULT_DATES,
    _futu_prices,
    _states,
)
from portfolio.market_data import record_security_price
from portfolio.models import (
    InvestmentAccount,
    PriceSourceChoices,
    Security,
    SecurityPriceRecord,
)


class Command(BaseCommand):
    help = "从 Futu 回填目标快照日期所需的历史收盘价，不覆盖已有人工价格。"

    def add_arguments(self, parser):
        parser.add_argument(
            "--date",
            action="append",
            dest="dates",
            help="目标日期 YYYY-MM-DD，可重复传入；默认使用历史快照目标日期。",
        )
        parser.add_argument(
            "--delay",
            type=float,
            default=1.05,
            help="逐标的请求间隔秒数，默认1.05秒。",
        )

    def handle(self, *args, **options):
        dates = []
        for raw in options["dates"] or []:
            parsed = parse_date(raw)
            if not parsed:
                raise CommandError(f"无效日期：{raw}")
            dates.append(parsed)
        dates = sorted(set(dates or DEFAULT_DATES))
        family = get_household_family()
        accounts = list(
            InvestmentAccount.objects.filter(
                pk__in=account_ids_as_of(
                    family, max(dates), include_ledger=False
                )
            )
            .select_related("bank_account__member")
        )
        states = _states(accounts, dates)
        required_dates = defaultdict(set)
        for on_date in dates:
            for state in states[on_date].values():
                for position in state["positions"]:
                    required_dates[position["security"].pk].add(on_date)

        prices, errors = _futu_prices(states, dates, options["delay"])
        securities = Security.objects.in_bulk(prices.keys())
        written = 0
        preserved = 0
        missing = []
        for security_id, target_dates in required_dates.items():
            available = prices.get(security_id, {})
            security = securities.get(security_id)
            if not security:
                continue
            selected_dates = set()
            for target_date in target_dates:
                candidates = [item for item in available if item <= target_date]
                if not candidates:
                    missing.append(f"{security.market}:{security.symbol}@{target_date}")
                    continue
                selected_dates.add(max(candidates))
            for price_date in sorted(selected_dates):
                if SecurityPriceRecord.objects.filter(
                    security=security,
                    price_as_of__date=price_date,
                ).exists():
                    preserved += 1
                    continue
                record_security_price(
                    security,
                    available[price_date],
                    source=PriceSourceChoices.FUTU,
                    price_type="close",
                    price_as_of=timezone.make_aware(
                        datetime.combine(price_date, time(16, 0))
                    ),
                    is_delayed=True,
                    quote_data={
                        "quote_time": price_date.isoformat(),
                        "raw_data": {"purpose": "portfolio_historical_snapshot"},
                    },
                )
                written += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"已保存 {written} 条历史收盘价；保留已有价格 {preserved} 条。"
            )
        )
        if missing:
            self.stdout.write(self.style.WARNING(f"Futu 缺少 {len(missing)} 个目标价格："))
            for item in missing:
                self.stdout.write(item)
        if errors:
            self.stdout.write(self.style.WARNING(f"不支持或查询失败 {len(errors)} 个标的："))
            for code, message in sorted(errors.items()):
                self.stdout.write(f"{code}\t{message}")
