import django.db.models.deletion

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("portfolio", "0011_unify_accounts_and_portfolio_accounting"),
    ]

    operations = [
        migrations.RemoveIndex(
            model_name="investmentaccount",
            name="portfolio_i_family__c95e94_idx",
        ),
        migrations.RemoveIndex(
            model_name="investmentaccount",
            name="portfolio_i_is_acti_8a4dc8_idx",
        ),
        migrations.RemoveField(model_name="investmentaccount", name="family"),
        migrations.RemoveField(model_name="investmentaccount", name="member"),
        migrations.RemoveField(model_name="investmentaccount", name="account_region"),
        migrations.RemoveField(model_name="investmentaccount", name="account_name"),
        migrations.RemoveField(model_name="investmentaccount", name="account_no_masked"),
        migrations.RemoveField(model_name="investmentaccount", name="visibility"),
        migrations.RemoveField(model_name="investmentaccount", name="is_active"),
        migrations.RemoveField(model_name="investmentaccount", name="remark"),
        migrations.AlterField(
            model_name="investmentaccount",
            name="bank_account",
            field=models.OneToOneField(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="investment_profile",
                to="ledger.bankaccount",
                verbose_name="关联账户",
            ),
        ),
        migrations.AddConstraint(
            model_name="investmentposition",
            constraint=models.UniqueConstraint(
                fields=("account", "security"),
                name="unique_current_investment_position",
            ),
        ),
        migrations.AddConstraint(
            model_name="portfoliosnapshot",
            constraint=models.UniqueConstraint(
                fields=("family", "member", "account", "snapshot_date", "currency"),
                name="unique_portfolio_snapshot_scope_date_currency",
                nulls_distinct=False,
            ),
        ),
        migrations.CreateModel(
            name="PortfolioSnapshotPositionLine",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("asset_type", models.CharField(max_length=30, verbose_name="资产类型")),
                ("asset_name", models.CharField(max_length=200, verbose_name="资产名称")),
                ("quantity", models.DecimalField(decimal_places=6, default=0, max_digits=24, verbose_name="数量")),
                ("price", models.DecimalField(decimal_places=6, default=0, max_digits=20, verbose_name="快照价格")),
                ("currency", models.CharField(max_length=10, verbose_name="原币")),
                ("fx_rate", models.DecimalField(decimal_places=8, default=1, max_digits=20, verbose_name="折算汇率")),
                ("market_value_original", models.DecimalField(decimal_places=4, default=0, max_digits=20, verbose_name="原币市值")),
                ("market_value", models.DecimalField(decimal_places=4, default=0, max_digits=20, verbose_name="本位币市值")),
                ("cost_original", models.DecimalField(decimal_places=4, default=0, max_digits=20, verbose_name="原币成本")),
                ("cost", models.DecimalField(decimal_places=4, default=0, max_digits=20, verbose_name="本位币成本")),
                ("unrealized_pnl", models.DecimalField(decimal_places=4, default=0, max_digits=20, verbose_name="本位币浮动盈亏")),
                ("account", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="snapshot_position_lines", to="portfolio.investmentaccount", verbose_name="投资账户")),
                ("security", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="snapshot_position_lines", to="portfolio.security", verbose_name="证券标的")),
                ("snapshot", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="position_lines", to="portfolio.portfoliosnapshot", verbose_name="组合快照")),
            ],
            options={
                "verbose_name": "组合快照持仓明细",
                "verbose_name_plural": "组合快照持仓明细",
                "ordering": ["account_id", "asset_type", "asset_name"],
            },
        ),
        migrations.AddIndex(
            model_name="portfoliosnapshotpositionline",
            index=models.Index(fields=["snapshot", "asset_type"], name="portfolio_p_snapsho_cd3c93_idx"),
        ),
    ]
