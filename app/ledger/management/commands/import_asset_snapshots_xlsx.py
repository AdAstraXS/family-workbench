from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from family_core.models import Family
from ledger.asset_snapshot_import import (
    AssetSnapshotWorkbookError,
    import_asset_snapshot_workbook,
)


class Command(BaseCommand):
    help = "从固定格式 Excel 的“账户余额NEW”工作表导入资产快照"

    def add_arguments(self, parser):
        parser.add_argument("xlsx_path")
        parser.add_argument("--family", default="我的家庭", help="家庭名称")
        parser.add_argument("--dry-run", action="store_true", help="校验后回滚，不写入数据库")

    def handle(self, *args, **options):
        try:
            family = Family.objects.get(name=options["family"])
        except Family.DoesNotExist as exc:
            raise CommandError(f"找不到家庭“{options['family']}”") from exc
        except Family.MultipleObjectsReturned as exc:
            raise CommandError(f"家庭名称“{options['family']}”不唯一") from exc

        try:
            with open(options["xlsx_path"], "rb") as source:
                with transaction.atomic():
                    result = import_asset_snapshot_workbook(
                        family=family,
                        source=source,
                        source_filename=source.name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1],
                    )
                    if options["dry_run"]:
                        transaction.set_rollback(True)
        except (OSError, AssetSnapshotWorkbookError) as exc:
            raise CommandError(str(exc)) from exc

        for summary in result.snapshot_summaries:
            member_text = "，".join(
                f"{member} {amount}" for member, amount in summary["member_totals"].items()
            )
            self.stdout.write(
                f"{summary['date']}：{summary['entry_count']} 条，"
                f"家庭合计 {summary['total_base']}（{member_text}）"
            )
        if result.created_dates:
            self.stdout.write("新增快照：" + "、".join(map(str, result.created_dates)))
        if result.verified_dates:
            self.stdout.write("核对通过：" + "、".join(map(str, result.verified_dates)))
        if result.historical_accounts_created:
            self.stdout.write(
                f"新增停用历史账户 {len(result.historical_accounts_created)} 个："
                + "、".join(result.historical_accounts_created)
            )
        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("试运行完成，所有数据库变更均已回滚"))
        else:
            self.stdout.write(self.style.SUCCESS("资产快照导入完成"))
