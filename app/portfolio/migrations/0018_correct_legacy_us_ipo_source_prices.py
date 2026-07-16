from decimal import Decimal, ROUND_HALF_UP

from django.db import migrations


RATE = Decimal("7.8")
MONEY = Decimal("0.0001")
STOCK_CODES = {"CHA.US", "FIGR.US", "GEMI.US", "BTGO.US"}
TRADE_IDS = {273, 274, 275, 276, 277, 278}


def converted(value):
    return (value / RATE).quantize(MONEY, rounding=ROUND_HALF_UP)


def correct_legacy_us_ipo_source_prices(apps, schema_editor):
    Listing = apps.get_model("ipo", "HkIpoListing")
    SubscriptionTrade = apps.get_model("ipo", "HkIpoSubscriptionTrade")

    for listing in Listing.objects.filter(stock_code__in=STOCK_CODES):
        if (listing.extra_data or {}).get("legacy_hkd_to_usd_rate"):
            continue
        listing.final_price = converted(listing.final_price)
        listing.entry_fee = converted(listing.entry_fee)
        listing.extra_data = {
            **(listing.extra_data or {}),
            "legacy_hkd_to_usd_rate": str(RATE),
        }
        listing.save(update_fields=["final_price", "entry_fee", "extra_data"])

    fields = (
        "application_amount",
        "financing_interest",
        "subscription_fee",
        "allotted_value",
        "allotment_fee",
        "sell_price",
        "trading_fee",
        "realized_profit",
    )
    trades = SubscriptionTrade.objects.filter(
        pk__in=TRADE_IDS,
        member__display_name="我",
        listing__stock_code__in=STOCK_CODES,
        account__account_name__in={"老虎证券", "富途证券"},
    )
    for trade in trades:
        if (trade.extra_data or {}).get("legacy_hkd_to_usd_rate"):
            continue
        for field in fields:
            setattr(trade, field, converted(getattr(trade, field)))
        trade.extra_data = {
            **(trade.extra_data or {}),
            "legacy_hkd_to_usd_rate": str(RATE),
        }
        trade.save(update_fields=[*fields, "extra_data"])


class Migration(migrations.Migration):
    dependencies = [
        ("ipo", "0017_remove_financing_calculation_fields"),
        ("portfolio", "0017_correct_legacy_us_ipo_currency"),
    ]

    operations = [
        migrations.RunPython(
            correct_legacy_us_ipo_source_prices,
            migrations.RunPython.noop,
        )
    ]
