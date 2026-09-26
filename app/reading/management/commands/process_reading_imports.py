from django.core.management.base import BaseCommand, CommandError
from reading.models import BookFile
from reading.services import process_file


class Command(BaseCommand):
    help = "处理已由成员上传并排队的图书；不扫描目录、不调用 AI。"

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=3)

    def handle(self, *args, **options):
        if not 1 <= options["limit"] <= 20:
            raise CommandError("limit 须在 1 到 20 之间。")
        failed = 0
        ids = list(BookFile.objects.filter(status="queued").order_by("created_at").values_list("pk", flat=True)[:options["limit"]])
        for pk in ids:
            process_file(pk)
            status = BookFile.objects.get(pk=pk).status
            self.stdout.write(f"file={pk} status={status}")
            failed += status == "failed"
        if failed:
            raise CommandError(f"{failed} 个图书处理失败，详细原因见图书详情。")
