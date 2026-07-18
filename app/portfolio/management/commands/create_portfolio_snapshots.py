from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django.utils.dateparse import parse_date

from family_core.household import get_household_family, get_site_setting
from portfolio.historical_valuation import account_ids_as_of
from portfolio.models import InvestmentAccount
from portfolio.snapshot_service import create_portfolio_snapshots_for_date
from portfolio.valuation import refresh_position_valuations


class Command(BaseCommand):
    help = "按指定日期重建账户、成员和家庭投资组合快照；重复执行会更新原快照。"

    def add_arguments(self, parser):
        parser.add_argument(
            "--date",
            action="append",
            dest="dates",
            help="快照日期 YYYY-MM-DD，可重复传入；默认今天。",
        )
        parser.add_argument(
            "--require-complete",
            action="store_true",
            help="存在缺价、缺汇率或流水错误时停止，不写入快照。",
        )

    def handle(self, *args, **options):
        family = get_household_family()
        if not family:
            self.stderr.write(self.style.WARNING("No household is configured."))
            return
        dates = []
        for raw in options["dates"] or []:
            parsed = parse_date(raw)
            if not parsed:
                raise CommandError(f"无效日期：{raw}")
            dates.append(parsed)
        dates = sorted(set(dates or [timezone.localdate()]))
        currency = get_site_setting().base_currency
        if timezone.localdate() in dates:
            refresh_position_valuations(on_date=timezone.localdate())

        count = 0
        for snapshot_date in dates:
            accounts = list(
                InvestmentAccount.objects.filter(
                    pk__in=account_ids_as_of(family, snapshot_date)
                )
                .select_related("bank_account__member")
            )
            try:
                snapshots = create_portfolio_snapshots_for_date(
                    family,
                    accounts,
                    snapshot_date,
                    currency,
                    require_complete=options["require_complete"],
                )
            except ValueError as exc:
                raise CommandError(str(exc)) from exc
            count += len(snapshots)
            family_snapshot = next(
                item
                for item in snapshots
                if item.member_id is None and item.account_id is None
            )
            status = "完整" if family_snapshot.extra_data.get("complete") else "存在缺项"
            self.stdout.write(
                f"{snapshot_date}：{len(snapshots)} 个范围，家庭总资产 "
                f"{family_snapshot.total_asset:.2f} {currency}（{status}）"
            )
        self.stdout.write(self.style.SUCCESS(f"已创建或更新 {count} 个快照。"))
