from django.db import migrations
from django.db.models import Q


PROVIDER_NAME = "智谱视觉识别"


def add_zhipu_vision_provider(apps, schema_editor):
    AiProvider = apps.get_model("ai_analysis", "AiProvider")
    existing_provider = (
        AiProvider.objects.filter(
            Q(base_url__icontains="open.bigmodel.cn")
            | Q(name__icontains="智谱")
            | Q(name__iexact="BigModel")
        )
        .order_by("-updated_at")
        .first()
    )
    if existing_provider:
        return

    AiProvider.objects.create(
        name=PROVIDER_NAME,
        provider_type="vision",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        model_name="glm-5v-turbo",
        is_active=True,
        extra_data={
            "api_key_env_var": "ZHIPU_API_KEY",
            "image_detail": "high",
        },
    )


def remove_zhipu_vision_provider(apps, schema_editor):
    AiProvider = apps.get_model("ai_analysis", "AiProvider")
    AiProvider.objects.filter(name=PROVIDER_NAME).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("ai_analysis", "0003_add_doubao_vision_provider"),
    ]

    operations = [
        migrations.RunPython(
            add_zhipu_vision_provider,
            remove_zhipu_vision_provider,
        ),
    ]
