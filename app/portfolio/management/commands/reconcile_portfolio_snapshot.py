from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.utils.dateparse import parse_date

from family_core.household import get_household_family
from ledger.models import AssetBalanceSnapshot
from portfolio.reconciliation import (
    apply_reconciliation,
    build_reconciliation_preview,
)


class Command(BaseCommand):
    help = "预览或执行家庭账本月底快照与投资账户余额的差额对齐。"

    def add_arguments(self, parser):
        parser.add_argument("--date", required=True, help="账本快照日期 YYYY-MM-DD")
        parser.add_argument(
            "--apply",
            action="store_true",
            help="实际写入调整现金流水；不传时只读预览。",
        )

    def handle(self, *args, **options):
        snapshot_date = parse_date(options["date"])
        if not snapshot_date:
            raise CommandError("日期格式必须为 YYYY-MM-DD。")
        family = get_household_family()
        snapshot = (
            AssetBalanceSnapshot.objects.filter(
                family=family,
                snapshot_date=snapshot_date,
                is_draft=False,
            )
            .order_by("-created_at", "-pk")
            .first()
        )
        if not snapshot:
            raise CommandError(f"找不到 {snapshot_date} 的非草稿家庭账本快照。")
        try:
            preview = build_reconciliation_preview(snapshot)
        except ValidationError as exc:
            raise CommandError("；".join(exc.messages)) from exc

        self.stdout.write(
            "成员\t投资账户\t账本本位币\t调整前试算\t差额\t调整原币\t状态/说明"
        )
        for row in preview.rows:
            self.stdout.write(
                "\t".join(
                    (
                        row.member_name,
                        row.account_name,
                        str(row.ledger_base_amount or ""),
                        str(row.calculated_base_amount or ""),
                        str(row.adjustment_base_amount or ""),
                        (
                            f"{row.adjustment_original_amount} {row.currency}"
                            if row.adjustment_original_amount is not None
                            else ""
                        ),
                        row.message or row.status,
                    )
                )
            )
        self.stdout.write(
            f"合计：账本 {preview.total_ledger:.2f}，"
            f"调整前 {preview.total_calculated:.2f}，"
            f"差额 {preview.total_adjustment:.2f} {snapshot.base_currency}"
        )
        if not options["apply"]:
            self.stdout.write("当前为 dry-run，未写入任何数据。")
            return
        if not preview.can_apply:
            raise CommandError("预览存在阻断项，未执行。")
        try:
            run = apply_reconciliation(snapshot)
        except (ValidationError, ValueError) as exc:
            messages = getattr(exc, "messages", [str(exc)])
            raise CommandError("；".join(messages)) from exc
        self.stdout.write(
            self.style.SUCCESS(
                f"已执行 {snapshot_date} 差额对齐，共 {run.lines.count()} 个账户。"
            )
        )
