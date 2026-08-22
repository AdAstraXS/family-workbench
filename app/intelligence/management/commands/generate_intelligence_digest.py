from datetime import date

from django.core.management.base import BaseCommand, CommandError

from family_core.models import Family
from intelligence.digest import IntelligenceDigestError, generate_daily_digest


class Command(BaseCommand):
    help = "根据已完成的 AI 结构化分析生成或更新家庭每日情报简报；本命令不会调用 AI。"

    def add_arguments(self, parser):
        parser.add_argument(
            "--family-id",
            type=int,
            required=True,
            help="需要生成简报的家庭 ID。",
        )
        parser.add_argument(
            "--date",
            dest="digest_date",
            help="简报日期，格式 YYYY-MM-DD；默认使用系统本地日期。",
        )

    def handle(self, *args, **options):
        family = Family.objects.filter(pk=options["family_id"]).first()
        if family is None:
            raise CommandError("指定的家庭不存在。")

        digest_date = None
        if options.get("digest_date"):
            try:
                digest_date = date.fromisoformat(options["digest_date"])
            except ValueError as exc:
                raise CommandError("--date 必须使用 YYYY-MM-DD 格式。") from exc

        try:
            digest, changed, run = generate_daily_digest(
                family=family,
                user=None,
                digest_date=digest_date,
            )
        except IntelligenceDigestError as exc:
            raise CommandError(str(exc)) from exc

        action = "已更新" if changed else "内容未变化"
        self.stdout.write(
            self.style.SUCCESS(
                f"简报 #{digest.pk}（{digest.digest_date}）{action}；"
                f"共 {digest.items.count()} 条，审计运行 #{run.pk}。"
            )
        )
