from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("portfolio", "0013_protect_investment_account_source")]

    operations = [
        migrations.AlterField(
            model_name="security",
            name="asset_type",
            field=models.CharField(
                choices=[
                    ("stock", "股票"),
                    ("etf", "ETF"),
                    ("bond", "债券"),
                    ("option", "期权"),
                    ("fund", "基金"),
                    ("other", "其他"),
                ],
                default="stock",
                max_length=30,
                verbose_name="金融品种",
            ),
        ),
        migrations.AddField(
            model_name="investmenttransaction",
            name="position_effect",
            field=models.CharField(
                blank=True,
                choices=[("open", "开仓"), ("close", "平仓")],
                max_length=10,
                verbose_name="开平仓",
            ),
        ),
        migrations.CreateModel(
            name="OptionContract",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                ("option_type", models.CharField(choices=[("call", "看涨"), ("put", "看跌")], max_length=10, verbose_name="期权类型")),
                ("strike_price", models.DecimalField(decimal_places=6, max_digits=20, verbose_name="行权价")),
                ("expiration_date", models.DateField(verbose_name="到期日")),
                ("multiplier", models.PositiveIntegerField(default=100, verbose_name="合约乘数")),
                ("security", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="option_contract", to="portfolio.security", verbose_name="期权合约标的")),
                ("underlying", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="option_contracts", to="portfolio.security", verbose_name="正股标的")),
            ],
            options={"verbose_name": "期权合约", "verbose_name_plural": "期权合约"},
        ),
        migrations.AddConstraint(
            model_name="optioncontract",
            constraint=models.UniqueConstraint(fields=("underlying", "option_type", "strike_price", "expiration_date"), name="unique_option_contract_terms"),
        ),
    ]
