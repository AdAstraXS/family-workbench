import django.core.validators
from django.db import migrations, models


def extend_default_quote_freshness(apps, schema_editor):
    WheelPolicy = apps.get_model("option_wheel", "WheelPolicy")
    WheelPolicy.objects.filter(
        quote_max_age_seconds=120,
        ruleset_version="m1-v1",
    ).update(
        quote_max_age_seconds=600,
        ruleset_version="m1-v2",
    )


def restore_default_quote_freshness(apps, schema_editor):
    WheelPolicy = apps.get_model("option_wheel", "WheelPolicy")
    WheelPolicy.objects.filter(
        quote_max_age_seconds=600,
        ruleset_version="m1-v2",
    ).update(
        quote_max_age_seconds=120,
        ruleset_version="m1-v1",
    )


class Migration(migrations.Migration):

    dependencies = [
        ("option_wheel", "0008_wheelanalysisjob"),
    ]

    operations = [
        migrations.AlterField(
            model_name="wheelpolicy",
            name="quote_max_age_seconds",
            field=models.PositiveIntegerField(
                default=600,
                validators=[django.core.validators.MinValueValidator(1)],
                verbose_name="报价最大年龄（秒）",
            ),
        ),
        migrations.AlterField(
            model_name="wheelpolicy",
            name="ruleset_version",
            field=models.CharField(default="m1-v2", max_length=30, verbose_name="规则集版本"),
        ),
        migrations.RunPython(
            extend_default_quote_freshness,
            restore_default_quote_freshness,
        ),
    ]
