from pathlib import Path

from django.core.files import File
from django.core.management.base import BaseCommand, CommandError

from family_core.models import Family
from ledger.expense_import import ExpenseWorkbookError, import_expense_workbook


class Command(BaseCommand):
    help = "按家庭账本固定格式导入支出 Excel。"

    def add_arguments(self, parser):
        parser.add_argument("xlsx_path")
        parser.add_argument("--family", default="我的家庭")

    def handle(self, *args, **options):
        path = Path(options["xlsx_path"])
        if not path.is_file():
            raise CommandError(f"文件不存在：{path}")
        try:
            family = Family.objects.get(name=options["family"])
        except Family.DoesNotExist as exc:
            raise CommandError(f"找不到家庭：{options['family']}") from exc

        try:
            with path.open("rb") as stream:
                result = import_expense_workbook(
                    family=family,
                    uploaded_file=File(stream, name=path.name),
                )
        except ExpenseWorkbookError as exc:
            raise CommandError(str(exc)) from exc

        batch = result.batch
        if result.duplicate_file:
            self.stdout.write(
                self.style.WARNING(
                    f"文件已导入过，未重复写入：批次 {batch.pk}，原新增 {batch.imported_count} 笔。"
                )
            )
            return
        self.stdout.write(
            self.style.SUCCESS(
                f"导入完成：批次 {batch.pk}，读取 {batch.row_count} 笔，新增 {batch.imported_count} 笔，"
                f"跳过 {batch.skipped_count} 笔，净支出 {batch.total_amount:.2f} 元。"
            )
        )
