from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("portfolio", "0029_investmenttransaction_option_purpose")]

    operations = [
        migrations.CreateModel(
            name="StockMarketResearchSnapshot",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("quote", models.JSONField(blank=True, default=dict)),
                ("candles", models.JSONField(blank=True, default=list)),
                ("valuation", models.JSONField(blank=True, default=dict)),
                ("analysts", models.JSONField(blank=True, default=dict)),
                ("morningstar", models.JSONField(blank=True, default=dict)),
                ("errors", models.JSONField(blank=True, default=dict)),
                ("fetched_at", models.DateTimeField(blank=True, null=True)),
                ("last_attempt_at", models.DateTimeField(blank=True, null=True)),
                ("security", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="stock_research_snapshot", to="portfolio.security")),
            ],
            options={"verbose_name": "个股行情与估值缓存", "verbose_name_plural": "个股行情与估值缓存"},
        ),
    ]
