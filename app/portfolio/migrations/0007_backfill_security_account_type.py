from django.db import migrations
from django.db.models import Q


def backfill_security_account_type(apps, schema_editor):
    account_model = apps.get_model("portfolio", "InvestmentAccount")
    account_type_model = apps.get_model("family_core", "AccountType")

    for account in account_model.objects.filter(account_type_ref=None):
        account_type = (
            account_type_model.objects.filter(
                Q(name__icontains="券商") | Q(name__icontains="证券"),
                family_id=account.family_id,
                is_active=True,
            )
            .order_by("display_order", "pk")
            .first()
            or account_type_model.objects.filter(
                Q(name__icontains="券商") | Q(name__icontains="证券"),
                family=None,
                is_active=True,
            )
            .order_by("display_order", "pk")
            .first()
        )
        if account_type:
            account.account_type_ref_id = account_type.pk
            account.save(update_fields=["account_type_ref"])


class Migration(migrations.Migration):
    dependencies = [
        ("portfolio", "0006_transaction_options_and_daily_rates"),
    ]

    operations = [
        migrations.RunPython(
            backfill_security_account_type,
            migrations.RunPython.noop,
        ),
    ]
