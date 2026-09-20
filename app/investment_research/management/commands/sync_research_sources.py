"""统一官方材料同步命令（M2A-4）。

只同步已有研究档案的不同证券；重复执行幂等；终端输出简短计数与
状态，不含原始正文/响应体/环境变量。供 NAS 定时任务调用：

    python manage.py sync_research_sources \
        [--symbol MSFT] [--source sec|microsoft_ir] \
        [--max-documents 20] [--fail-on-error]
"""
from django.core.management.base import BaseCommand, CommandError

from investment_research.source_sync import (
    SYNC_SOURCE_CHOICES,
    sync_research_sources,
)


class Command(BaseCommand):
    help = (
        "同步已有研究档案证券的官方材料（SEC / Microsoft IR）；"
        "幂等，输出简短计数。"
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--symbol",
            action="append",
            default=None,
            help=(
                "只同步指定代码（大小写不敏感精确匹配，可重复）；"
                "默认同步全部已有研究档案的证券。"
            ),
        )
        parser.add_argument(
            "--source",
            action="append",
            default=None,
            choices=list(SYNC_SOURCE_CHOICES),
            help="只同步指定来源（可重复）；默认两个来源。",
        )
        parser.add_argument(
            "--max-documents",
            type=int,
            default=20,
            help="每个来源最多同步的文档数（1..100，默认 20）。",
        )
        parser.add_argument(
            "--fail-on-error",
            action="store_true",
            help="存在任何失败时以非零退出码结束。",
        )

    def handle(self, *args, **options):
        try:
            results, totals = sync_research_sources(
                symbols=options["symbol"],
                sources=options["source"],
                max_documents=options["max_documents"],
            )
        except ValueError as exc:
            # 筛选/参数错误：在任何 provider 调用前失败
            raise CommandError(str(exc))

        for entry in results:
            if entry["status"] == "ok":
                counters = entry["counters"]
                self.stdout.write(
                    f"{entry['security']} {entry['source']}: "
                    f"created={counters['created']} "
                    f"updated={counters['updated']} "
                    f"unchanged={counters['unchanged']} "
                    f"failed={counters['failed']}"
                )
            else:
                self.stdout.write(
                    f"{entry['security']} {entry['source']}: "
                    f"{entry['status']}（{entry['reason']}）"
                )
        self.stdout.write(
            "汇总: created={created} updated={updated} unchanged={unchanged} "
            "skipped={skipped} failed={failed}".format(**totals)
        )

        if options["fail_on_error"] and totals["failed"] > 0:
            raise CommandError(
                f"同步完成但存在 {totals['failed']} 个失败项。"
            )
