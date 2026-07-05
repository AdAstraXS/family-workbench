from django.db import migrations, models


def initialize_diluted_cost(apps, schema_editor):
    InvestmentPosition = apps.get_model("portfolio", "InvestmentPosition")
    for position in InvestmentPosition.objects.all().iterator():
        position.diluted_cost = position.avg_cost
        position.save(update_fields=["diluted_cost"])


class Migration(migrations.Migration):
    dependencies = [
        ("portfolio", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="investmentposition",
            name="diluted_cost",
            field=models.DecimalField(
                decimal_places=6,
                default=0,
                max_digits=20,
                verbose_name="摊薄成本",
            ),
        ),
        migrations.AddField(
            model_name="investmentposition",
            name="realized_pnl",
            field=models.DecimalField(
                decimal_places=4,
                default=0,
                max_digits=20,
                verbose_name="累计已实现盈亏",
            ),
        ),
        migrations.AddField(
            model_name="investmenttransaction",
            name="cash_change",
            field=models.DecimalField(
                decimal_places=4,
                default=0,
                max_digits=20,
                verbose_name="现金变动",
            ),
        ),
        migrations.AddField(
            model_name="investmenttransaction",
            name="external_id",
            field=models.CharField(
                blank=True,
                max_length=200,
                verbose_name="外部流水号",
            ),
        ),
        migrations.AddField(
            model_name="investmenttransaction",
            name="realized_return_ratio",
            field=models.DecimalField(
                decimal_places=6,
                default=0,
                max_digits=12,
                verbose_name="已实现收益率",
            ),
        ),
        migrations.AddField(
            model_name="investmenttransaction",
            name="sell_cost",
            field=models.DecimalField(
                decimal_places=4,
                default=0,
                max_digits=20,
                verbose_name="卖出成本",
            ),
        ),
        migrations.AddField(
            model_name="investmenttransaction",
            name="source",
            field=models.CharField(
                choices=[
                    ("manual", "手工录入"),
                    ("import", "文件导入"),
                    ("futu", "Futu 同步"),
                ],
                default="manual",
                max_length=20,
                verbose_name="数据来源",
            ),
        ),
        migrations.AddField(
            model_name="investmenttransaction",
            name="status",
            field=models.CharField(
                choices=[
                    ("planned", "计划中"),
                    ("submitted", "已提交"),
                    ("partial", "部分成交"),
                    ("completed", "已成交"),
                    ("cancelled", "已取消"),
                ],
                default="completed",
                max_length=20,
                verbose_name="交易状态",
            ),
        ),
        migrations.AlterField(
            model_name="investmenttransaction",
            name="trade_type",
            field=models.CharField(
                choices=[
                    ("buy", "买入"),
                    ("sell", "卖出"),
                    ("dividend", "分红"),
                    ("interest", "利息"),
                    ("fee", "手续费"),
                    ("tax", "税费"),
                    ("deposit", "入金"),
                    ("withdrawal", "出金"),
                    ("transfer", "转仓"),
                    ("split", "拆合股"),
                    ("other", "其他"),
                ],
                max_length=30,
                verbose_name="交易类型",
            ),
        ),
        migrations.RunPython(
            initialize_diluted_cost,
            migrations.RunPython.noop,
        ),
        migrations.AddConstraint(
            model_name="investmenttransaction",
            constraint=models.UniqueConstraint(
                condition=~models.Q(external_id=""),
                fields=("account", "source", "external_id"),
                name="unique_portfolio_external_transaction",
            ),
        ),
    ]
