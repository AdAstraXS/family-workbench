import django.db.models.deletion
from django.db import migrations, models


OPTION_ROWS = {
    "transaction_type": [
        ("buy", "买入"),
        ("ipo", "打新"),
        ("sell", "卖出"),
        ("dividend", "分红"),
        ("interest", "利息"),
        ("other_fee_adjustment", "其他费用调整"),
        ("other", "其他"),
    ],
    "information_source": [
        ("a", "A级：公司的财报、公告、优质行业分析、券商研报"),
        ("b", "B级：行业大V经过靠谱分析后的推荐"),
        ("c", "C级：朋友的内部消息"),
        ("d", "D级：微信群、论坛、朋友小道消息"),
    ],
    "strategy_type": [
        ("short_speculation", "短期投机"),
        ("mid_swing", "中期波段"),
        ("long_value", "长期价值"),
        ("short_connect", "短期入通"),
        ("other", "其他"),
    ],
    "emotion": [
        ("fear", "恐惧"),
        ("anxiety", "焦虑"),
        ("calm", "平静"),
        ("joy", "欣喜"),
        ("confidence", "自信"),
        ("greed", "贪婪"),
    ],
}


def seed_options_and_identifiers(apps, schema_editor):
    option_model = apps.get_model("portfolio", "InvestmentOption")
    transaction_model = apps.get_model("portfolio", "InvestmentTransaction")
    account_model = apps.get_model("portfolio", "InvestmentAccount")
    account_type_model = apps.get_model("family_core", "AccountType")

    options = {}
    for category, rows in OPTION_ROWS.items():
        for order, (code, name) in enumerate(rows, 1):
            item, _ = option_model.objects.update_or_create(
                category=category,
                code=code,
                defaults={"name": name, "sort_order": order, "is_active": True},
            )
            options[(category, code)] = item

    serials = {}
    for transaction in transaction_model.objects.order_by(
        "trade_date", "trade_type", "pk"
    ):
        trade_type = transaction.trade_type
        if trade_type not in dict(OPTION_ROWS["transaction_type"]):
            trade_type = "other"
            transaction.trade_type = trade_type
        transaction.trade_type_option_id = options[
            ("transaction_type", trade_type)
        ].pk
        key = (transaction.trade_date, trade_type)
        serials[key] = serials.get(key, 0) + 1
        transaction.transaction_no = (
            f"{transaction.trade_date:%Y%m%d}-{trade_type.upper()}-"
            f"{serials[key]:04d}"
        )
        transaction.save(
            update_fields=[
                "trade_type",
                "trade_type_option",
                "transaction_no",
            ]
        )

    for account in account_model.objects.filter(account_type_ref=None):
        account_type = (
            account_type_model.objects.filter(
                family_id=account.family_id,
                name__icontains="证券",
            ).first()
            or account_type_model.objects.filter(
                family=None,
                name__icontains="证券",
            ).first()
        )
        if account_type:
            account.account_type_ref_id = account_type.pk
            account.save(update_fields=["account_type_ref"])


class Migration(migrations.Migration):
    dependencies = [
        ("family_core", "0002_accountregion_accounttype_assetcategory"),
        ("portfolio", "0005_account_cash_and_trade_journal"),
    ]

    operations = [
        migrations.CreateModel(
            name="DailyExchangeRateFetch",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("fetch_date", models.DateField(unique=True, verbose_name="抓取日期")),
                ("source_date", models.DateField(blank=True, null=True, verbose_name="汇率日期")),
                ("status", models.CharField(default="success", max_length=20, verbose_name="状态")),
                ("error_message", models.TextField(blank=True, verbose_name="错误信息")),
                ("fetched_at", models.DateTimeField(auto_now=True, verbose_name="抓取时间")),
            ],
            options={
                "verbose_name": "每日汇率抓取",
                "verbose_name_plural": "每日汇率抓取",
            },
        ),
        migrations.CreateModel(
            name="InvestmentOption",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                ("category", models.CharField(choices=[("transaction_type", "交易类型"), ("information_source", "信息来源"), ("strategy_type", "交易策略"), ("emotion", "交易情绪")], max_length=30, verbose_name="选项类别")),
                ("code", models.SlugField(max_length=50, verbose_name="选项代码")),
                ("name", models.CharField(max_length=200, verbose_name="显示名称")),
                ("sort_order", models.PositiveSmallIntegerField(default=0, verbose_name="排序")),
                ("is_active", models.BooleanField(default=True, verbose_name="启用")),
            ],
            options={
                "verbose_name": "投资交易选项",
                "verbose_name_plural": "投资交易选项",
                "ordering": ["category", "sort_order", "pk"],
            },
        ),
        migrations.AddConstraint(
            model_name="investmentoption",
            constraint=models.UniqueConstraint(
                fields=("category", "code"),
                name="unique_investment_option_code",
            ),
        ),
        migrations.AddField(
            model_name="investmentaccount",
            name="account_type_ref",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="investment_accounts",
                to="family_core.accounttype",
                verbose_name="账户类型",
            ),
        ),
        migrations.AddField(
            model_name="investmenttransaction",
            name="asset_category",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="investment_transactions",
                to="family_core.assetcategory",
                verbose_name="资产类别",
            ),
        ),
        migrations.AddField(
            model_name="investmenttransaction",
            name="emotion_option",
            field=models.ForeignKey(
                blank=True,
                limit_choices_to={"category": "emotion"},
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="emotion_transactions",
                to="portfolio.investmentoption",
                verbose_name="交易情绪选项",
            ),
        ),
        migrations.AddField(
            model_name="investmenttransaction",
            name="information_source_option",
            field=models.ForeignKey(
                blank=True,
                limit_choices_to={"category": "information_source"},
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="source_transactions",
                to="portfolio.investmentoption",
                verbose_name="信息来源选项",
            ),
        ),
        migrations.AddField(
            model_name="investmenttransaction",
            name="strategy_option",
            field=models.ForeignKey(
                blank=True,
                limit_choices_to={"category": "strategy_type"},
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="strategy_transactions",
                to="portfolio.investmentoption",
                verbose_name="交易策略选项",
            ),
        ),
        migrations.AddField(
            model_name="investmenttransaction",
            name="strategy_other",
            field=models.CharField(blank=True, max_length=100, verbose_name="其他交易策略"),
        ),
        migrations.AddField(
            model_name="investmenttransaction",
            name="trade_type_option",
            field=models.ForeignKey(
                blank=True,
                limit_choices_to={"category": "transaction_type"},
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="typed_transactions",
                to="portfolio.investmentoption",
                verbose_name="交易类型选项",
            ),
        ),
        migrations.AddField(
            model_name="investmenttransaction",
            name="transaction_no",
            field=models.CharField(blank=True, editable=False, max_length=40, null=True, unique=True, verbose_name="交易编号"),
        ),
        migrations.AlterField(
            model_name="investmenttransaction",
            name="currency",
            field=models.CharField(blank=True, default="", max_length=10, verbose_name="币种"),
        ),
        migrations.AlterField(
            model_name="investmenttransaction",
            name="trade_type",
            field=models.CharField(choices=[("buy", "买入"), ("ipo", "打新"), ("sell", "卖出"), ("dividend", "分红"), ("interest", "利息"), ("other_fee_adjustment", "其他费用调整"), ("other", "其他")], max_length=30, verbose_name="交易类型"),
        ),
        migrations.RunPython(
            seed_options_and_identifiers,
            migrations.RunPython.noop,
        ),
    ]
