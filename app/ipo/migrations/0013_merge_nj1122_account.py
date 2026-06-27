from django.db import migrations


def merge_nj1122_account(apps, schema_editor):
    bank_account_model = apps.get_model("ledger", "BankAccount")
    balance_entry_model = apps.get_model("ledger", "AssetBalanceEntry")
    income_record_model = apps.get_model("ledger", "IncomeRecord")
    expense_record_model = apps.get_model("ledger", "ExpenseRecord")
    trade_model = apps.get_model("ipo", "HkIpoSubscriptionTrade")

    target = (
        bank_account_model.objects.filter(
            account_name="信诚NJ1122",
            member__display_name="孙秘书",
        )
        .select_related("member")
        .first()
    )
    if target is None:
        return

    sources = bank_account_model.objects.filter(
        account_name="信诚NJ1122",
        family_id=target.family_id,
    ).exclude(pk=target.pk)

    for source in sources:
        trade_model.objects.filter(account_id=source.pk).update(
            account_id=target.pk,
            member_id=target.member_id,
        )
        income_record_model.objects.filter(bank_account_id=source.pk).update(
            bank_account_id=target.pk,
            member_id=target.member_id,
        )
        expense_record_model.objects.filter(bank_account_id=source.pk).update(
            bank_account_id=target.pk,
            member_id=target.member_id,
        )

        for entry in balance_entry_model.objects.filter(account_id=source.pk):
            target_entry = (
                balance_entry_model.objects.filter(
                    account_id=target.pk,
                    snapshot_id=entry.snapshot_id,
                    asset_category_id=entry.asset_category_id,
                    currency=entry.currency,
                )
                .exclude(pk=entry.pk)
                .first()
            )
            if target_entry is None:
                entry.account_id = target.pk
                entry.member_id = target.member_id
                entry.account_name = target.account_name
                entry.save(
                    update_fields=["account", "member", "account_name", "updated_at"]
                )
                continue

            target_entry.original_amount += entry.original_amount
            target_entry.base_amount += entry.base_amount
            target_entry.display_order = min(
                target_entry.display_order,
                entry.display_order,
            )
            target_entry.save(
                update_fields=[
                    "original_amount",
                    "base_amount",
                    "display_order",
                    "updated_at",
                ]
            )
            entry.delete()

        source.delete()


class Migration(migrations.Migration):
    dependencies = [
        ("ipo", "0012_add_unallotted_trade_status"),
        ("ledger", "0005_annualbudget_annualbudgetline_and_more"),
    ]

    operations = [
        migrations.RunPython(merge_nj1122_account, migrations.RunPython.noop),
    ]
