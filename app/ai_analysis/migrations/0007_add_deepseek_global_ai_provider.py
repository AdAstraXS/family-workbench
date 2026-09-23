from django.db import migrations


PROVIDER_NAME = "DeepSeek 全局 AI"


def add_provider(apps, schema_editor):
    AiProvider = apps.get_model("ai_analysis", "AiProvider")
    AiProvider.objects.update_or_create(
        name=PROVIDER_NAME,
        defaults={
            "provider_type": "openai_compatible",
            "base_url": "https://api.deepseek.com",
            "model_name": "deepseek-v4-pro",
            "execution_location": "cloud",
            "is_active": True,
            "extra_data": {
                "global_ai_enabled": True,
                "api_key_env_var": "KNOWLEDGE_TEXT_AI_API_KEY",
                "global_ai_input_usd_per_million": "1.32",
                "global_ai_output_usd_per_million": "3.96",
                "global_ai_max_estimated_usd": "0.10",
                "global_ai_max_input_characters": 20000,
                "global_ai_max_output_tokens": 2000,
                "global_ai_max_http_requests": 4,
                "global_ai_timeout_seconds": 45,
                "global_ai_daily_request_limit": 20,
            },
        },
    )


def remove_provider(apps, schema_editor):
    apps.get_model("ai_analysis", "AiProvider").objects.filter(name=PROVIDER_NAME).delete()


class Migration(migrations.Migration):
    dependencies = [("ai_analysis", "0006_aianswershare")]
    operations = [migrations.RunPython(add_provider, remove_provider)]
