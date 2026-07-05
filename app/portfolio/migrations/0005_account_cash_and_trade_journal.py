import django.db.models.deletion
from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    dependencies = [
        ("family_core", "0002_accountregion_accounttype_assetcategory"),
        ("portfolio", "0004_securitymarketsnapshot_change_rate_and_ps_ratio"),
    ]

    operations = [
        migrations.AddField(
            model_name="security",
            name="asset_category",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="securities",
                to="family_core.assetcategory",
                verbose_name="一级资产类别",
            ),
        ),
        migrations.AddField(
            model_name="investmenttransaction",
            name="emotion",
            field=models.CharField(blank=True, max_length=30, verbose_name="交易情绪"),
        ),
        migrations.AddField(
            model_name="investmenttransaction",
            name="exit_condition",
            field=models.TextField(blank=True, verbose_name="退出条件"),
        ),
        migrations.AddField(
            model_name="investmenttransaction",
            name="information_source",
            field=models.CharField(blank=True, max_length=200, verbose_name="信息来源"),
        ),
        migrations.AddField(
            model_name="investmenttransaction",
            name="strategy_type",
            field=models.CharField(blank=True, max_length=50, verbose_name="交易策略"),
        ),
        migrations.AddField(
            model_name="investmenttransaction",
            name="trade_logic",
            field=models.TextField(blank=True, verbose_name="交易逻辑"),
        ),
        migrations.CreateModel(
            name="InvestmentCashMovement",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                ("movement_date", models.DateField(verbose_name="发生日期")),
                ("settlement_date", models.DateField(blank=True, null=True, verbose_name="结算日期")),
                ("movement_type", models.CharField(choices=[("deposit", "入金"), ("withdrawal", "出金"), ("buy", "买入"), ("sell", "卖出"), ("dividend", "股息"), ("interest", "利息"), ("fee", "费用"), ("tax", "税费"), ("exchange", "换汇"), ("transfer", "转账"), ("adjustment", "余额调整")], max_length=30, verbose_name="变动类型")),
                ("currency", models.CharField(max_length=10, verbose_name="币种")),
                ("amount", models.DecimalField(decimal_places=4, max_digits=20, verbose_name="变动金额")),
                ("source", models.CharField(choices=[("manual", "手工录入"), ("import", "文件导入"), ("futu", "Futu 同步")], default="manual", max_length=20, verbose_name="数据来源")),
                ("external_id", models.CharField(blank=True, max_length=200, verbose_name="外部流水号")),
                ("remark", models.TextField(blank=True, verbose_name="备注")),
                ("account", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="cash_movements", to="portfolio.investmentaccount", verbose_name="投资账户")),
                ("transaction", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="cash_movement", to="portfolio.investmenttransaction", verbose_name="关联交易")),
            ],
            options={
                "verbose_name": "投资账户现金流水",
                "verbose_name_plural": "投资账户现金流水",
                "ordering": ["movement_date", "created_at", "pk"],
            },
        ),
        migrations.AddIndex(
            model_name="investmentcashmovement",
            index=models.Index(fields=["account", "currency", "movement_date"], name="portfolio_i_account_3fead5_idx"),
        ),
        migrations.AddIndex(
            model_name="investmentcashmovement",
            index=models.Index(fields=["movement_type", "movement_date"], name="portfolio_i_movemen_8ad3bc_idx"),
        ),
        migrations.AddConstraint(
            model_name="investmentcashmovement",
            constraint=models.UniqueConstraint(
                condition=~Q(external_id=""),
                fields=("account", "source", "external_id"),
                name="unique_portfolio_external_cash_movement",
            ),
        ),
    ]
