from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("portfolio", "0025_dailyportfoliovaluationrun"),
    ]

    operations = [
        migrations.AddField(
            model_name="portfoliosnapshotpositionline",
            name="fx_rate_as_of",
            field=models.DateField(
                blank=True,
                null=True,
                verbose_name="汇率日期",
            ),
        ),
        migrations.AddField(
            model_name="portfoliosnapshotpositionline",
            name="price_as_of",
            field=models.DateField(
                blank=True,
                null=True,
                verbose_name="价格日期",
            ),
        ),
        migrations.AddField(
            model_name="portfoliosnapshotpositionline",
            name="price_source",
            field=models.CharField(
                blank=True,
                max_length=30,
                verbose_name="价格来源",
            ),
        ),
        migrations.AddField(
            model_name="portfoliosnapshotpositionline",
            name="pricing_status",
            field=models.CharField(
                blank=True,
                choices=[
                    ("fresh", "最新"),
                    ("manual", "手工价格"),
                    ("stale", "价格过期"),
                    ("missing", "缺少价格"),
                    ("error", "刷新失败"),
                    ("legacy", "历史价格"),
                    ("expired_unresolved", "到期未处理"),
                ],
                max_length=30,
                verbose_name="价格状态",
            ),
        ),
    ]
