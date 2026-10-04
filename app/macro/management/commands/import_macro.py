from django.core.management.base import BaseCommand, CommandError

from macro.adapters import SourceError
from macro.registry import GROUPS, OFFICIAL_GROUPS
from macro.services import import_group


class Command(BaseCommand):
    help = "采集宏观数据，默认只读试算。--write 才写入；失败返回非零。"

    def add_arguments(self, parser):
        parser.add_argument("--group", action="append", choices=sorted(GROUPS))
        parser.add_argument("--all-structured", action="store_true", help="全部已登记结构化接口；不含官方发布稿")
        parser.add_argument("--url", default="", help="对应官方发布稿 HTTPS 地址；只允许一个官方采集组")
        parser.add_argument("--write", action="store_true")
        parser.add_argument("--list", action="store_true")

    def handle(self, *args, **options):
        if options["list"]:
            for group, specs in sorted(GROUPS.items()):
                self.stdout.write(group + ": " + "、".join(s.name for s in specs))
            return
        groups = sorted(set(options["group"] or []))
        if options["all_structured"]:
            groups = sorted(set(groups) | (set(GROUPS) - OFFICIAL_GROUPS))
        if not groups:
            raise CommandError("请选择 --group 或 --all-structured；--list 可列出采集组")
        if (set(groups) & OFFICIAL_GROUPS) and (len(groups) != 1 or not options["url"]):
            raise CommandError("官方发布稿须单组运行并指定 --url")
        if options["url"] and not (set(groups) & OFFICIAL_GROUPS):
            raise CommandError("结构化接口不接受 --url")
        self.stdout.write("写入模式" if options["write"] else "只读试算（不写数据库）")
        failures = []
        for group in groups:
            try:
                summary = import_group(group, write=options["write"], url=options["url"])
                self.stdout.write(f"{group}: {summary}")
            except SourceError as exc:
                failures.append(group)
                self.stderr.write(f"{group}: {exc}")
        if failures:
            raise CommandError("失败采集组：" + ", ".join(failures))
