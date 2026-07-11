from django.db import migrations, models


def seed_site_setting(apps, schema_editor):
    Family = apps.get_model("family_core", "Family")
    FamilyMember = apps.get_model("family_core", "FamilyMember")
    SiteSetting = apps.get_model("family_core", "SiteSetting")
    family = Family.objects.order_by("pk").first()
    SiteSetting.objects.update_or_create(
        pk=1,
        defaults={
            "household_name": family.name if family else "家庭工作台",
            "base_currency": family.base_currency if family else "CNY",
            "timezone": "Asia/Shanghai",
        },
    )
    for order, member in enumerate(FamilyMember.objects.order_by("pk"), start=1):
        FamilyMember.objects.filter(pk=member.pk).update(display_order=order)


class Migration(migrations.Migration):
    dependencies = [
        ("family_core", "0003_familymember_display_order"),
    ]

    operations = [
        migrations.CreateModel(
            name="SiteSetting",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                ("household_name", models.CharField(default="家庭工作台", max_length=100, verbose_name="工作台名称")),
                ("base_currency", models.CharField(default="CNY", max_length=10, verbose_name="默认本位币")),
                ("timezone", models.CharField(default="Asia/Shanghai", max_length=50, verbose_name="时区")),
            ],
            options={"verbose_name": "站点设置", "verbose_name_plural": "站点设置"},
        ),
        migrations.RunPython(seed_site_setting, migrations.RunPython.noop),
    ]
