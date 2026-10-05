import json
from datetime import date

from django.core.management.base import BaseCommand, CommandError

from macro.adapters import SourceError
from macro.discovery import CATALOGUES
from macro.maintenance import maintain


class Command(BaseCommand):
    help = "宏观维护：官方报告发现、结构化更新、发布日历。默认只读；定时任务须显式 --write。"

    def add_arguments(self, parser):
        parser.add_argument("--mode", choices=["official", "structured", "calendar", "all"], default="official")
        parser.add_argument("--write", action="store_true")
        parser.add_argument("--start", type=date.fromisoformat, help="历史回补起始统计期，YYYY-MM-DD")
        parser.add_argument("--end", type=date.fromisoformat, help="历史回补结束统计期，YYYY-MM-DD")
        parser.add_argument("--pages", type=int, default=5, help="每个官方目录最多读取页数，1–80")
        parser.add_argument("--group", action="append", choices=list(CATALOGUES))

    def handle(self, *args, **options):
        if bool(options["start"]) != bool(options["end"]):
            raise CommandError("历史回补须同时指定起止日期")
        if options["start"] and (options["start"] > options["end"] or options["mode"] != "official"):
            raise CommandError("起止日期须有序，且仅适用于官方报告模式")
        if not 1 <= options["pages"] <= 80:
            raise CommandError("分页上限须在1–80之间")
        if options["group"] and options["mode"] != "official":
            raise CommandError("--group 仅适用于官方报告模式")
        try:
            result = maintain(options["mode"], write=options["write"], start=options["start"], end=options["end"],
                              pages=options["pages"], groups=options["group"])
        except SourceError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(result, ensure_ascii=False, indent=2))
        if result["failures"]:
            raise CommandError(f'{len(result["failures"])} 项来源未完成；已保留此前有效数据')
