from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from intelligence.models import IntelligenceSource


class Command(BaseCommand):
    help = "为明确选择的 RSS 信源启用公开网页必要证据提取。"

    def add_arguments(self, parser):
        parser.add_argument("--source-id", type=int, action="append", required=True)
        parser.add_argument(
            "--confirm-public-html",
            action="store_true",
            help="确认不登录、不使用 Cookie、不绕过付费墙且不保存完整正文。",
        )

    def handle(self, *args, **options):
        if not options["confirm_public_html"]:
            raise CommandError("必须显式确认 --confirm-public-html。")
        source_ids = list(dict.fromkeys(options["source_id"]))
        sources = list(IntelligenceSource.objects.filter(pk__in=source_ids).order_by("pk"))
        if len(sources) != len(source_ids):
            raise CommandError("至少一个指定信源不存在，未修改任何配置。")
        invalid = [source for source in sources if source.adapter_key != IntelligenceSource.ADAPTER_RSS]
        if invalid:
            raise CommandError("公开网页证据提取目前只能启用于 RSS / Atom 信源。")
        with transaction.atomic():
            for source in sources:
                extra_data = dict(source.extra_data or {})
                extra_data["article_fetch_policy"] = IntelligenceSource.ARTICLE_FETCH_PUBLIC_HTML
                source.extra_data = extra_data
                source.save(update_fields=["extra_data", "updated_at"])
        labels = "、".join(f"#{source.pk} {source.name}" for source in sources)
        self.stdout.write(self.style.SUCCESS(f"已启用公开网页证据提取：{labels}。"))
