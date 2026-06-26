from datetime import timedelta

from django.db import migrations


def populate_missing_closed_sell_dates(apps, schema_editor):
    trade_model = apps.get_model("ipo", "HkIpoSubscriptionTrade")
    trades = trade_model.objects.filter(trade_status="closed", sell_date__isnull=True).select_related("listing")
    for trade in trades.iterator():
        result_date = trade.listing.allotment_result_date
        fallback_date = trade.listing.subscription_end_date
        if result_date:
            trade.sell_date = result_date
        elif fallback_date:
            trade.sell_date = fallback_date + timedelta(days=2)
        else:
            continue
        trade.save(update_fields=["sell_date"])


class Migration(migrations.Migration):

    dependencies = [
        ("ipo", "0007_hkiposubscriptiontrade_sell_date"),
    ]

    operations = [
        migrations.RunPython(populate_missing_closed_sell_dates, migrations.RunPython.noop),
    ]
