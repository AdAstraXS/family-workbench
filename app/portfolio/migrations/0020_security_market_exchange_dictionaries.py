import django.db.models.deletion
from django.db import migrations, models


def seed_market_dictionaries(apps, schema_editor):
    SecurityMarket = apps.get_model("portfolio", "SecurityMarket")
    SecurityExchange = apps.get_model("portfolio", "SecurityExchange")

    markets = [
        ("HK", "港股", "HKD", True, 10, "香港证券市场"),
        ("US", "美股", "USD", True, 20, "美国证券市场"),
        ("CN", "A 股", "CNY", True, 30, "中国内地人民币普通股票市场"),
        (
            "CN_B",
            "B 股",
            "",
            True,
            40,
            "上海 B 股以 USD 计价，深圳 B 股以 HKD 计价。",
        ),
        ("OTHER", "其他市场", "", False, 90, "场外或暂未标准化的市场"),
    ]
    market_objects = {}
    for code, name, currency, supports_futu, order, remark in markets:
        market, _ = SecurityMarket.objects.update_or_create(
            code=code,
            defaults={
                "name": name,
                "default_currency": currency,
                "supports_futu": supports_futu,
                "display_order": order,
                "is_active": True,
                "remark": remark,
            },
        )
        market_objects[code] = market

    exchanges = [
        ("HK", "HK", "香港交易所", "HKD", "HK", 10, ""),
        ("US", "US", "美国市场（未细分）", "USD", "US", 10, "兼容现有数据"),
        ("US", "NASDAQ", "纳斯达克", "USD", "US", 20, ""),
        ("US", "NYSE", "纽约证券交易所", "USD", "US", 30, ""),
        ("US", "AMEX", "NYSE American", "USD", "US", 40, ""),
        ("US", "OTC", "美国场外市场", "USD", "", 50, "通常使用手工行情配置"),
        ("CN", "SH", "上海证券交易所", "CNY", "SH", 10, "A 股"),
        ("CN", "SZ", "深圳证券交易所", "CNY", "SZ", 20, "A 股"),
        ("CN", "BJ", "北京证券交易所", "CNY", "", 30, "行情代码按实际供应商配置"),
        ("CN_B", "SH", "上海 B 股", "USD", "SH", 10, "股票代码通常以 900 开头"),
        ("CN_B", "SZ", "深圳 B 股", "HKD", "SZ", 20, "股票代码通常以 200 开头"),
        ("OTHER", "OTHER", "其他 / 场外", "", "", 90, ""),
    ]
    for market_code, code, name, currency, prefix, order, remark in exchanges:
        SecurityExchange.objects.update_or_create(
            market=market_objects[market_code],
            code=code,
            defaults={
                "name": name,
                "default_currency": currency,
                "provider_prefix": prefix,
                "display_order": order,
                "is_active": True,
                "remark": remark,
            },
        )


class Migration(migrations.Migration):
    dependencies = [
        ("portfolio", "0019_market_data_foundation"),
    ]

    operations = [
        migrations.CreateModel(
            name="SecurityMarket",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                ("code", models.CharField(max_length=20, unique=True, verbose_name="市场代码")),
                ("name", models.CharField(max_length=100, verbose_name="市场名称")),
                ("default_currency", models.CharField(blank=True, max_length=10, verbose_name="默认币种")),
                ("supports_futu", models.BooleanField(default=False, verbose_name="支持 Futu 行情")),
                ("display_order", models.PositiveSmallIntegerField(default=0, verbose_name="显示顺序")),
                ("is_active", models.BooleanField(default=True, verbose_name="启用")),
                ("remark", models.CharField(blank=True, max_length=300, verbose_name="说明")),
            ],
            options={
                "verbose_name": "证券市场字典",
                "verbose_name_plural": "证券市场字典",
                "ordering": ["display_order", "code"],
            },
        ),
        migrations.CreateModel(
            name="SecurityExchange",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                ("code", models.CharField(max_length=30, verbose_name="交易所代码")),
                ("name", models.CharField(max_length=100, verbose_name="交易所名称")),
                ("default_currency", models.CharField(blank=True, max_length=10, verbose_name="默认币种")),
                ("provider_prefix", models.CharField(blank=True, help_text="例如 Futu 使用 HK、US、SH、SZ；不自动取行情时可留空。", max_length=20, verbose_name="行情源前缀")),
                ("display_order", models.PositiveSmallIntegerField(default=0, verbose_name="显示顺序")),
                ("is_active", models.BooleanField(default=True, verbose_name="启用")),
                ("remark", models.CharField(blank=True, max_length=300, verbose_name="说明")),
                ("market", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="exchanges", to="portfolio.securitymarket", verbose_name="所属市场")),
            ],
            options={
                "verbose_name": "证券交易所字典",
                "verbose_name_plural": "证券交易所字典",
                "ordering": ["market__display_order", "display_order", "code"],
                "constraints": [models.UniqueConstraint(fields=("market", "code"), name="unique_security_exchange_market_code")],
            },
        ),
        migrations.RunPython(seed_market_dictionaries, migrations.RunPython.noop),
    ]
