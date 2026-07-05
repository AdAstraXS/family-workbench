from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("portfolio", "0003_watchlist_and_market_snapshot"),
    ]

    operations = [
        migrations.AddField(
            model_name="securitymarketsnapshot",
            name="change_rate",
            field=models.DecimalField(
                blank=True,
                decimal_places=6,
                max_digits=20,
                null=True,
                verbose_name="当日涨跌幅",
            ),
        ),
        migrations.AddField(
            model_name="securitymarketsnapshot",
            name="ps_ratio",
            field=models.DecimalField(
                blank=True,
                decimal_places=6,
                max_digits=20,
                null=True,
                verbose_name="市销率",
            ),
        ),
    ]
