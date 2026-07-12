import django.db.models.deletion
from django.db import migrations, models


def sync_broker_accounts(apps, schema_editor):
    investment_account_model = apps.get_model(
        "portfolio",
        "InvestmentAccount",
    )
    bank_account_model = apps.get_model("ledger", "BankAccount")

    bank_accounts = bank_account_model.objects.filter(
        account_type_ref__name="券商",
    ).select_related("member")
    for bank_account in bank_accounts:
        account = investment_account_model.objects.filter(
            bank_account_id=bank_account.pk,
        ).first()
        if not account:
            account = investment_account_model.objects.filter(
                bank_account=None,
                family_id=bank_account.family_id,
                member_id=bank_account.member_id,
                account_name=bank_account.account_name,
            ).first()
        if not account:
            account = investment_account_model(
                bank_account_id=bank_account.pk,
                broker_name=bank_account.account_name,
                market_scope="",
                currency="CNY",
                cash_balance=0,
            )
        account.bank_account_id = bank_account.pk
        account.family_id = bank_account.family_id
        account.member_id = bank_account.member_id
        account.account_type_ref_id = bank_account.account_type_ref_id
        account.account_name = bank_account.account_name
        account.account_no_masked = bank_account.account_no_masked
        account.account_region_id = bank_account.account_region_id
        account.is_active = bank_account.is_active
        account.save()


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("family_core", "0002_accountregion_accounttype_assetcategory"),
        ("ledger", "0003_alter_assetbalanceentry_options_and_more"),
        ("portfolio", "0008_link_investment_account_to_bank_account"),
    ]

    operations = [
        migrations.AddField(
            model_name="investmentaccount",
            name="account_region",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="investment_accounts",
                to="family_core.accountregion",
                verbose_name="账户地区",
            ),
        ),
        migrations.AddField(
            model_name="investmentcashmovement",
            name="counterparty_account",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="investment_cash_counterparties",
                to="ledger.bankaccount",
                verbose_name="对手账户",
            ),
        ),
        migrations.RunPython(
            sync_broker_accounts,
            migrations.RunPython.noop,
        ),
        migrations.RemoveIndex(
            model_name="investmentaccount",
            name="portfolio_i_broker__a01731_idx",
        ),
        migrations.RemoveField(
            model_name="investmentaccount",
            name="account_type_ref",
        ),
        migrations.RemoveField(
            model_name="investmentaccount",
            name="broker_name",
        ),
        migrations.RemoveField(
            model_name="investmentaccount",
            name="cash_balance",
        ),
        migrations.RemoveField(
            model_name="investmentaccount",
            name="currency",
        ),
        migrations.RemoveField(
            model_name="investmentaccount",
            name="market_scope",
        ),
    ]
