from django.db import migrations, models


def add_wvr_listing_type(apps, schema_editor):
    option_model = apps.get_model("ipo", "HkIpoListingOption")
    option_model.objects.get_or_create(
        category="listing_type",
        code="wvr",
        defaults={
            "name": "同股不同权",
            "sort_order": 50,
            "is_active": True,
        },
    )


class Migration(migrations.Migration):

    dependencies = [
        ("ipo", "0009_hkipolistingoption"),
    ]

    operations = [
        migrations.AlterField(
            model_name="hkipolisting",
            name="hk_connect_threshold_100m",
            field=models.DecimalField(
                blank=True,
                decimal_places=4,
                max_digits=20,
                null=True,
                verbose_name="港股通门槛（亿港元）",
            ),
        ),
        migrations.RunPython(add_wvr_listing_type, migrations.RunPython.noop),
    ]
