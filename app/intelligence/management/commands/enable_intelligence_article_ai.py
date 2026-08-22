from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from ai_analysis.models import AiProvider
from intelligence.ai_enrichment import (
    INTELLIGENCE_ARTICLE_DATA_SCOPE,
    INTELLIGENCE_ARTICLE_POLICY_VERSION,
)


class Command(BaseCommand):
    help = "为现有文本 AI 明确启用公开网页必要证据摘录（不含完整正文、登录或付费内容）。"

    def add_arguments(self, parser):
        parser.add_argument("--provider-id", type=int, required=True)
        parser.add_argument(
            "--confirm-public-article-snippets",
            action="store_true",
            help="确认只发送公开网页必要证据摘录，不发送完整正文或受限内容。",
        )

    def handle(self, *args, **options):
        if not options["confirm_public_article_snippets"]:
            raise CommandError("必须显式确认 --confirm-public-article-snippets。")
        provider = AiProvider.objects.filter(pk=options["provider_id"]).first()
        if provider is None:
            raise CommandError("找不到指定 AI 服务商。")
        extra_data = dict(provider.extra_data or {})
        if extra_data.get("allow_intelligence_analysis") is not True:
            raise CommandError("该服务商尚未启用 AI 情报分析，不能扩大数据范围。")
        required_limits = {
            "intelligence_max_input_characters",
            "intelligence_max_output_tokens",
            "intelligence_input_usd_per_million",
            "intelligence_output_usd_per_million",
            "intelligence_max_estimated_usd",
        }
        if not required_limits.issubset(extra_data):
            raise CommandError("该服务商缺少既有输入、输出或费用上限配置。")
        extra_data.update(
            {
                "intelligence_data_scope": INTELLIGENCE_ARTICLE_DATA_SCOPE,
                "intelligence_policy_version": INTELLIGENCE_ARTICLE_POLICY_VERSION,
                "intelligence_policy_reviewed_on": timezone.localdate().isoformat(),
            }
        )
        provider.extra_data = extra_data
        provider.save(update_fields=["extra_data", "updated_at"])
        self.stdout.write(
            self.style.SUCCESS(
                f"服务商 #{provider.pk} 已启用公开网页必要证据摘录；API Key 未读取或改写。"
            )
        )
