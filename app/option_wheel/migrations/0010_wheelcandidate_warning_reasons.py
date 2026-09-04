from decimal import Decimal

import django.core.validators
from django.db import migrations, models


def update_shared_policy_defaults(apps, schema_editor):
    Policy = apps.get_model("option_wheel", "WheelPolicy")
    Policy.objects.filter(
        preferred_premium_min=Decimal("200"),
        preferred_premium_max=Decimal("400"),
        preferred_dte_min=4,
        preferred_dte_max=9,
        max_spread_ratio=Decimal("0.15"),
        min_open_interest=100,
        min_volume=10,
        ruleset_version="m1-v2",
    ).update(
        preferred_premium_min=Decimal("90"),
        preferred_premium_max=Decimal("500"),
        preferred_dte_min=7,
        preferred_dte_max=30,
        max_spread_ratio=Decimal("1"),
        min_open_interest=0,
        min_volume=0,
        ruleset_version="decision-v1",
    )


class Migration(migrations.Migration):
    dependencies = [
        ("option_wheel", "0009_extend_option_quote_freshness"),
    ]

    operations = [
        migrations.AddField(
            model_name="wheelcandidate",
            name="warning_reasons",
            field=models.JSONField(blank=True, default=list, verbose_name="风险提示"),
        ),
        migrations.AlterField(
            model_name="wheelpolicy",
            name="preferred_premium_min",
            field=models.DecimalField(decimal_places=4, default=Decimal("90"), max_digits=20, validators=[django.core.validators.MinValueValidator(Decimal("0"))], verbose_name="最低权利金"),
        ),
        migrations.AlterField(
            model_name="wheelpolicy",
            name="preferred_premium_max",
            field=models.DecimalField(decimal_places=4, default=Decimal("500"), max_digits=20, validators=[django.core.validators.MinValueValidator(Decimal("0"))], verbose_name="最高权利金"),
        ),
        migrations.AlterField(
            model_name="wheelpolicy",
            name="preferred_dte_min",
            field=models.PositiveIntegerField(default=7, validators=[django.core.validators.MinValueValidator(1)], verbose_name="最短到期天数"),
        ),
        migrations.AlterField(
            model_name="wheelpolicy",
            name="preferred_dte_max",
            field=models.PositiveIntegerField(default=30, validators=[django.core.validators.MinValueValidator(1)], verbose_name="最长到期天数"),
        ),
        migrations.AlterField(
            model_name="wheelpolicy",
            name="max_spread_ratio",
            field=models.DecimalField(decimal_places=6, default=Decimal("1"), max_digits=10, validators=[django.core.validators.MinValueValidator(Decimal("0")), django.core.validators.MaxValueValidator(Decimal("1"))], verbose_name="最大买卖价差比例"),
        ),
        migrations.AlterField(
            model_name="wheelpolicy",
            name="min_open_interest",
            field=models.PositiveIntegerField(default=0, validators=[django.core.validators.MinValueValidator(0)], verbose_name="最小未平仓量"),
        ),
        migrations.AlterField(
            model_name="wheelpolicy",
            name="min_volume",
            field=models.PositiveIntegerField(default=0, validators=[django.core.validators.MinValueValidator(0)], verbose_name="最小成交量"),
        ),
        migrations.AlterField(
            model_name="wheelpolicy",
            name="ruleset_version",
            field=models.CharField(default="decision-v1", max_length=30, verbose_name="规则集版本"),
        ),
        migrations.RunPython(update_shared_policy_defaults, migrations.RunPython.noop),
    ]
