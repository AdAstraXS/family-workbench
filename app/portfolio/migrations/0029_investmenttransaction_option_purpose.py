from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("portfolio", "0028_optioncontract_is_adjusted_optioncontract_provider_and_more")]

    operations = [
        migrations.AddField(
            model_name="investmenttransaction",
            name="option_purpose",
            field=models.CharField(
                blank=True,
                choices=[
                    ("wheel_short_put", "车轮策略 · 卖出 Put"),
                    ("wheel_covered_call", "车轮策略 · 备兑 Call"),
                    ("protective_put", "保护已有正股 · 买入 Put"),
                    ("long_call", "长期看涨 · 买入 Call"),
                    ("long_put", "独立看跌 · 买入 Put"),
                    ("other_option", "其他期权用途"),
                ],
                max_length=30,
                verbose_name="期权用途",
            ),
        ),
    ]
