from django.db import migrations, models


def mark_zero_allotments_as_unallotted(apps, schema_editor):
    trade_model = apps.get_model("ipo", "HkIpoSubscriptionTrade")
    trade_model.objects.filter(
        trade_status="closed",
        allotted_lots=0,
    ).update(trade_status="unallotted")


def restore_zero_allotments_to_closed(apps, schema_editor):
    trade_model = apps.get_model("ipo", "HkIpoSubscriptionTrade")
    trade_model.objects.filter(
        trade_status="unallotted",
        allotted_lots=0,
    ).update(trade_status="closed")


class Migration(migrations.Migration):
    dependencies = [
        ("ipo", "0011_normalize_wvr_listing_type"),
    ]

    operations = [
        migrations.AlterField(
            model_name="hkiposubscriptiontrade",
            name="trade_status",
            field=models.CharField(
                choices=[
                    ("applying", "申购中"),
                    ("holding", "尚持有"),
                    ("closed", "清仓"),
                    ("unallotted", "未中签"),
                ],
                default="applying",
                max_length=20,
                verbose_name="新股状态",
            ),
        ),
        migrations.RunPython(
            mark_zero_allotments_as_unallotted,
            restore_zero_allotments_to_closed,
        ),
    ]
