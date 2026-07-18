from django.db import migrations


def add_arca_exchange(apps, schema_editor):
    SecurityExchange = apps.get_model("portfolio", "SecurityExchange")
    SecurityMarket = apps.get_model("portfolio", "SecurityMarket")
    us_market = SecurityMarket.objects.get(code="US")
    SecurityExchange.objects.update_or_create(
        market=us_market,
        code="ARCA",
        defaults={
            "name": "纽约证券交易所 Arca",
            "default_currency": "USD",
            "provider_prefix": "US",
            "display_order": 45,
            "is_active": True,
            "remark": "主要用于 ETF 等交易所交易产品",
        },
    )


class Migration(migrations.Migration):
    dependencies = [
        ("portfolio", "0021_reclassify_legacy_b_shares"),
    ]

    operations = [
        migrations.RunPython(add_arca_exchange, migrations.RunPython.noop),
    ]
