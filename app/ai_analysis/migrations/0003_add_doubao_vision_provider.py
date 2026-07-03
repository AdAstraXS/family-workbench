from django.db import migrations


PROVIDER_NAME = "豆包视觉识别（火山方舟）"


def add_doubao_vision_provider(apps, schema_editor):
    AiProvider = apps.get_model("ai_analysis", "AiProvider")
    AiProvider.objects.get_or_create(
        name=PROVIDER_NAME,
        defaults={
            "provider_type": "vision",
            "base_url": "https://ark.cn-beijing.volces.com/api/v3",
            "model_name": "doubao-seed-2-0-lite-260215",
            "is_active": True,
            "extra_data": {
                "api_key_env_var": "ARK_API_KEY",
                "image_detail": "high",
            },
        },
    )


def remove_doubao_vision_provider(apps, schema_editor):
    AiProvider = apps.get_model("ai_analysis", "AiProvider")
    AiProvider.objects.filter(name=PROVIDER_NAME).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("ai_analysis", "0002_remove_provider_api_keys"),
    ]

    operations = [
        migrations.RunPython(
            add_doubao_vision_provider,
            remove_doubao_vision_provider,
        ),
    ]
