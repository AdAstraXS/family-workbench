import django.db.models.deletion
from django.db import migrations, models


def configure_defaults(apps, schema_editor):
    Provider = apps.get_model("ai_analysis", "AiProvider")
    Setting = apps.get_model("ai_analysis", "AiModuleModel")
    flash = Provider.objects.filter(name="DeepSeek 文本整理", provider_type="openai_compatible").first()
    if flash and flash.base_url.rstrip("/") in {"https://api.deepseek.com", "https://api.deepseek.com/v1"}:
        extra = dict(flash.extra_data or {})
        flash.model_name = "deepseek-flash"
        extra.setdefault("api_key_env_var", "KNOWLEDGE_TEXT_AI_API_KEY")
        extra.update({
            "intelligence_input_usd_per_million": "0.30",
            "intelligence_output_usd_per_million": "1.20",
            "intelligence_disable_thinking": True,
        })
        pro_rows = Provider.objects.filter(name="DeepSeek 全局 AI", model_name="deepseek-v4-pro")
        pro = next((item for item in pro_rows if (item.extra_data or {}).get("allow_research_analysis") is True), None)
        pro_extra = (pro.extra_data or {}) if pro else {}
        research_keys = (
            "allow_research_analysis", "research_policy_version", "research_policy_reviewed_on",
            "research_max_input_chars", "research_max_output_tokens", "research_max_estimated_usd",
        )
        if pro_extra.get("allow_research_analysis") is True:
            extra.update({key: pro_extra[key] for key in research_keys if key in pro_extra})
            extra.update({
                "research_input_usd_per_million": "0.30",
                "research_output_usd_per_million": "1.20",
            })
        flash.extra_data = extra
        flash.save(update_fields=["model_name", "extra_data", "updated_at"])
        for module in ("option_wheel", "intelligence", "knowledge", "global_ai"):
            Setting.objects.update_or_create(module=module, defaults={"provider": flash})
        if extra.get("allow_research_analysis") is True:
            Setting.objects.update_or_create(module="investment_research", defaults={"provider": flash})
        elif pro and pro_extra.get("allow_research_analysis") is True:
            Setting.objects.update_or_create(module="investment_research", defaults={"provider": pro})

    # Public metadata only. Private research/knowledge scopes require separate approval.
    Provider.objects.get_or_create(
        name="智谱 GLM-5.3 FlashX（备用）",
        defaults={
            "provider_type": "openai_compatible",
            "base_url": "https://open.bigmodel.cn/api/paas/v4",
            "model_name": "glm-5.3-flashx",
            "execution_location": "cloud",
            "is_active": True,
            "extra_data": {
                "usage": "text", "api_key_env_var": "ZHIPU_API_KEY",
                "allow_intelligence_analysis": True,
                "intelligence_data_scope": "public_metadata_only",
                "intelligence_policy_version": "public-metadata-v1",
                "intelligence_policy_reviewed_on": "2026-09-24",
                "intelligence_max_input_characters": 12000,
                "intelligence_max_output_tokens": 4096,
                # Deliberately conservative USD cost gates for CNY-billed API.
                "intelligence_input_usd_per_million": "1.00",
                "intelligence_output_usd_per_million": "2.00",
                "intelligence_max_estimated_usd": "0.05",
            },
        },
    )


class Migration(migrations.Migration):
    dependencies = [("ai_analysis", "0010_align_request_field_defaults")]

    operations = [
        migrations.CreateModel(
            name="AiModuleModel",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                ("module", models.CharField(choices=[
                    ("option_wheel", "期权分析建议"),
                    ("investment_research", "投资研究"),
                    ("intelligence", "AI 情报"),
                    ("knowledge", "知识整理"),
                    ("global_ai", "全局 AI（尚未启用）"),
                ], max_length=40, unique=True, verbose_name="分析模块")),
                ("provider", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,
                    to="ai_analysis.aiprovider", verbose_name="默认文字模型")),
            ],
            options={"verbose_name": "模块默认模型", "verbose_name_plural": "模块默认模型"},
        ),
        migrations.RunPython(configure_defaults, migrations.RunPython.noop),
    ]
