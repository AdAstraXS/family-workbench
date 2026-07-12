import django.db.models.deletion
from django.db import migrations, models


def link_existing_accounts(apps, schema_editor):
    investment_account_model = apps.get_model(
        "portfolio",
        "InvestmentAccount",
    )
    bank_account_model = apps.get_model("ledger", "BankAccount")

    for account in investment_account_model.objects.filter(bank_account=None):
        bank_account = bank_account_model.objects.filter(
            family_id=account.family_id,
            member_id=account.member_id,
            account_name=account.account_name,
            account_type_ref__name="券商",
        ).first()
        if bank_account:
            account.bank_account_id = bank_account.pk
            account.save(update_fields=["bank_account"])


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0003_alter_assetbalanceentry_options_and_more"),
        ("portfolio", "0007_backfill_security_account_type"),
    ]

    operations = [
        migrations.AddField(
            model_name="investmentaccount",
            name="bank_account",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="investment_profile",
                to="ledger.bankaccount",
                verbose_name="关联账户",
            ),
        ),
        migrations.RunPython(
            link_existing_accounts,
            migrations.RunPython.noop,
        ),
    ]
