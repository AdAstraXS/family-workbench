from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("investment_research", "0009_officialresearchcontentversion_acquisition_note"),
    ]

    operations = [
        migrations.CreateModel(
            name="FutuFinancialSnapshot",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("provider_code", models.CharField(max_length=40, verbose_name="富途代码")),
                ("data", models.JSONField(default=dict, verbose_name="年度报表和收入构成")),
                ("fetched_at", models.DateTimeField(verbose_name="获取时间")),
                ("last_error", models.CharField(blank=True, max_length=500, verbose_name="最近刷新错误")),
                ("security", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="futu_financial_snapshot", to="portfolio.security", verbose_name="证券标的")),
            ],
            options={"verbose_name": "富途财务资料快照", "verbose_name_plural": "富途财务资料快照"},
        ),
    ]
