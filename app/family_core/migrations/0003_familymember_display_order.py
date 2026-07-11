from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("family_core", "0002_accountregion_accounttype_assetcategory"),
    ]

    operations = [
        migrations.AddField(
            model_name="familymember",
            name="display_order",
            field=models.PositiveIntegerField(default=0, verbose_name="显示顺序"),
        ),
        migrations.AlterModelOptions(
            name="familymember",
            options={
                "ordering": ["display_order", "id"],
                "verbose_name": "家庭成员",
                "verbose_name_plural": "家庭成员",
            },
        ),
    ]
