from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("portfolio", "0023_portfolioaccountbalanceanchor"),
    ]

    operations = [
        migrations.AlterField(
            model_name="investmentcashmovement",
            name="source",
            field=models.CharField(choices=[("manual", "手工录入"), ("import", "文件导入"), ("futu", "Futu 同步"), ("ipo", "港股打新"), ("reconciliation", "账本差额对齐")], default="manual", max_length=20, verbose_name="数据来源"),
        ),
        migrations.AlterField(
            model_name="investmenttransaction",
            name="source",
            field=models.CharField(choices=[("manual", "手工录入"), ("import", "文件导入"), ("futu", "Futu 同步"), ("ipo", "港股打新"), ("reconciliation", "账本差额对齐")], default="manual", max_length=20, verbose_name="数据来源"),
        ),
        migrations.CreateModel(
            name="PortfolioReconciliationRun",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                ("base_currency", models.CharField(default="CNY", max_length=10, verbose_name="本位币")),
                ("status", models.CharField(choices=[("applied", "已执行"), ("reverted", "已撤销")], default="applied", max_length=20, verbose_name="状态")),
                ("applied_at", models.DateTimeField(blank=True, null=True, verbose_name="执行时间")),
                ("reverted_at", models.DateTimeField(blank=True, null=True, verbose_name="撤销时间")),
                ("report", models.JSONField(blank=True, default=dict, verbose_name="核对报告")),
                ("applied_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="portfolio_reconciliations_applied", to=settings.AUTH_USER_MODEL, verbose_name="执行人")),
                ("family", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="portfolio_reconciliation_runs", to="family_core.family", verbose_name="所属家庭")),
                ("ledger_snapshot", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="portfolio_reconciliation_run", to="ledger.assetbalancesnapshot", verbose_name="家庭账本资产快照")),
                ("reverted_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="portfolio_reconciliations_reverted", to=settings.AUTH_USER_MODEL, verbose_name="撤销人")),
            ],
            options={
                "verbose_name": "投资账户差额对齐批次",
                "verbose_name_plural": "投资账户差额对齐批次",
                "ordering": ["-ledger_snapshot__snapshot_date", "-pk"],
            },
        ),
        migrations.CreateModel(
            name="PortfolioReconciliationLine",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                ("currency", models.CharField(max_length=10, verbose_name="调整币种")),
                ("ledger_base_amount", models.DecimalField(decimal_places=4, max_digits=24, verbose_name="账本本位币余额")),
                ("calculated_base_amount", models.DecimalField(decimal_places=4, max_digits=24, verbose_name="调整前试算余额")),
                ("adjustment_base_amount", models.DecimalField(decimal_places=4, max_digits=24, verbose_name="本位币调整额")),
                ("adjustment_original_amount", models.DecimalField(decimal_places=4, max_digits=24, verbose_name="原币调整额")),
                ("account", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="reconciliation_lines", to="portfolio.investmentaccount", verbose_name="投资账户")),
                ("movement", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="reconciliation_line", to="portfolio.investmentcashmovement", verbose_name="调整现金流水")),
                ("run", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="lines", to="portfolio.portfolioreconciliationrun", verbose_name="对齐批次")),
            ],
            options={
                "verbose_name": "投资账户差额对齐明细",
                "verbose_name_plural": "投资账户差额对齐明细",
                "ordering": ["account_id"],
            },
        ),
        migrations.AddConstraint(
            model_name="portfolioreconciliationline",
            constraint=models.UniqueConstraint(fields=("run", "account"), name="unique_portfolio_reconciliation_account"),
        ),
    ]
