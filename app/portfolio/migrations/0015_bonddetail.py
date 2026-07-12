from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("portfolio", "0014_option_contract_and_position_effect"),
    ]

    operations = [
        migrations.CreateModel(
            name="BondDetail",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                ("isin", models.CharField(blank=True, max_length=20, verbose_name="ISIN")),
                ("issuer", models.CharField(blank=True, max_length=200, verbose_name="发行人")),
                ("bond_type", models.CharField(choices=[("government", "政府债券"), ("corporate", "公司债券"), ("convertible", "可转换债券"), ("other", "其他债券")], default="government", max_length=20, verbose_name="债券类型")),
                ("face_value", models.DecimalField(decimal_places=4, default=100, max_digits=20, verbose_name="单张面值")),
                ("coupon_rate", models.DecimalField(decimal_places=6, default=0, max_digits=10, verbose_name="票面利率（%）")),
                ("coupon_frequency", models.PositiveSmallIntegerField(default=2, verbose_name="每年付息次数")),
                ("maturity_date", models.DateField(blank=True, null=True, verbose_name="到期日")),
                ("redemption_price", models.DecimalField(decimal_places=6, default=100, max_digits=20, verbose_name="到期兑付价格")),
                ("quote_basis", models.CharField(choices=[("per_100", "每 100 面值报价"), ("per_unit", "每单位报价")], default="per_100", max_length=20, verbose_name="报价方式")),
                ("accrued_interest", models.DecimalField(decimal_places=6, default=0, max_digits=20, verbose_name="每报价单位应计利息")),
                ("valuation_date", models.DateField(blank=True, null=True, verbose_name="估值日期")),
                ("security", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="bond_detail", to="portfolio.security", verbose_name="债券标的")),
            ],
            options={
                "verbose_name": "债券详情",
                "verbose_name_plural": "债券详情",
            },
        ),
    ]
