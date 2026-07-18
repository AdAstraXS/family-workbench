from django.db import migrations


def reclassify_legacy_b_shares(apps, schema_editor):
    Security = apps.get_model("portfolio", "Security")
    Security.objects.filter(
        market="CN",
        exchange="SH",
        asset_type="stock",
        symbol__startswith="900",
    ).update(market="CN_B", currency="USD")
    Security.objects.filter(
        market="CN",
        exchange="SZ",
        asset_type="stock",
        symbol__startswith="200",
    ).update(market="CN_B", currency="HKD")


class Migration(migrations.Migration):
    dependencies = [
        ("portfolio", "0020_security_market_exchange_dictionaries"),
    ]

    operations = [
        migrations.RunPython(reclassify_legacy_b_shares, migrations.RunPython.noop),
    ]
