from django.core.management.base import BaseCommand, CommandError

from intelligence.collection import collect_intelligence_sources
from intelligence.models import CollectionRun


class Command(BaseCommand):
    help = "采集到期的 RSS 与 YouTube 官方频道元数据，并生成待复核候选事件。"

    def add_arguments(self, parser):
        parser.add_argument(
            "--source-id",
            action="append",
            type=int,
            dest="source_ids",
            help="只采集指定信源，可重复填写。",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="忽略采集间隔，强制检查所选信源。",
        )
        parser.add_argument(
            "--max-items",
            type=int,
            default=50,
            help="每个信源本次最多处理条目数（1–100，默认 50）。",
        )

    def handle(self, *args, **options):
        max_items = options["max_items"]
        if not 1 <= max_items <= 100:
            raise CommandError("--max-items 必须在 1 到 100 之间。")
        run = collect_intelligence_sources(
            source_ids=options.get("source_ids"),
            due_only=not options["force"],
            max_items=max_items,
        )
        summary = (
            f"运行 #{run.pk}：发现 {run.discovered_count}，新增 {run.created_count}，"
            f"更新 {run.updated_count}，重复 {run.ignored_count}，噪音 {run.noise_count}，"
            f"候选 {run.clustered_count}，失败 {run.failed_count}。"
        )
        if run.status == CollectionRun.STATUS_SUCCESS:
            self.stdout.write(self.style.SUCCESS(summary))
            return
        self.stderr.write(self.style.ERROR(summary))
        if run.error_summary:
            self.stderr.write(run.error_summary)
        raise CommandError("采集未完全成功；已成功的其他信源和条目仍已保存。")
