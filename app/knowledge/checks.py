from django.conf import settings
from django.core.checks import Warning, register


@register()
def knowledge_configuration_checks(app_configs, **kwargs):
    warnings = []
    if not (
        settings.KNOWLEDGE_MICROSOFT_CLIENT_ID
        and settings.KNOWLEDGE_MICROSOFT_CLIENT_SECRET
    ):
        warnings.append(
            Warning(
                "Microsoft OneNote 授权尚未配置，知识中心仍可浏览随手记，"
                "但不能绑定或同步 OneNote。",
                hint=(
                    "配置 KNOWLEDGE_MICROSOFT_CLIENT_ID 和 "
                    "KNOWLEDGE_MICROSOFT_CLIENT_SECRET。"
                ),
                id="knowledge.W001",
            )
        )
    if not settings.KNOWLEDGE_TOKEN_ENCRYPTION_KEY:
        warnings.append(
            Warning(
                "知识底座令牌加密密钥尚未配置。",
                hint=(
                    "生产部署前设置 KNOWLEDGE_TOKEN_ENCRYPTION_KEY；"
                    "DEBUG 下只会使用不可迁移的开发密钥。"
                ),
                id="knowledge.W002",
            )
        )
    return warnings
