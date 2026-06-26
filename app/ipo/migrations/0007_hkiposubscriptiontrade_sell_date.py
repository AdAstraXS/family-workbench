from datetime import timedelta

from django.db import migrations, models


def populate_sell_dates(apps, schema_editor):
    trade_model = apps.get_model("ipo", "HkIpoSubscriptionTrade")
    trades = trade_model.objects.filter(sold_lots__gt=0, sell_date__isnull=True).select_related(
        "listing"
    )
    for trade in trades.iterator():
        base_date = trade.listing.subscription_end_date or trade.application_date
        if base_date:
            trade.sell_date = base_date + timedelta(days=2)
            trade.save(update_fields=["sell_date"])


class Migration(migrations.Migration):

    dependencies = [
        ("ipo", "0006_alter_hkiposubscriptiontrade_account"),
    ]

    operations = [
        migrations.AddField(
            model_name="hkiposubscriptiontrade",
            name="sell_date",
            field=models.DateField(blank=True, null=True, verbose_name="卖出日期"),
        ),
        migrations.AddIndex(
            model_name="hkiposubscriptiontrade",
            index=models.Index(fields=["sell_date"], name="ipo_hkiposu_sell_da_246070_idx"),
        ),
        migrations.RunPython(populate_sell_dates, migrations.RunPython.noop),
    ]
