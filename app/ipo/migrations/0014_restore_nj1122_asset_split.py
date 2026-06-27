from decimal import Decimal

from django.db import migrations


BALANCE_SPLIT = {
    "股票": {
        "owner_original": Decimal("5516.4179"),
        "owner_base": Decimal("4775.0113"),
        "secretary_original": Decimal("3723.5821"),
        "secretary_base": Decimal("3223.1327"),
    },
    "股指基金": {
        "owner_original": Decimal("28680.5970"),
        "owner_base": Decimal("24825.9248"),
        "secretary_original": Decimal("19359.4030"),
        "secretary_base": Decimal("16757.4992"),
    },
    "打新-现金": {
        "owner_original": Decimal("400748.0597"),
        "owner_base": Decimal("346887.5205"),
        "secretary_original": Decimal("270504.9403"),
        "secretary_base": Decimal("234149.0763"),
    },
}


def restore_nj1122_asset_split(apps, schema_editor):
    bank_account_model = apps.get_model("ledger", "BankAccount")
    balance_entry_model = apps.get_model("ledger", "AssetBalanceEntry")
    member_model = apps.get_model("family_core", "FamilyMember")

    secretary_account = (
        bank_account_model.objects.filter(
            account_name="信诚NJ1122",
            member__display_name="孙秘书",
        )
        .select_related("member")
        .first()
    )
    if secretary_account is None:
        return

    owner = member_model.objects.filter(
        family_id=secretary_account.family_id,
        display_name="我",
    ).first()
    if owner is None:
        return

    owner_account = bank_account_model.objects.filter(
        family_id=secretary_account.family_id,
        member_id=owner.pk,
        account_name="信诚NJ1122",
    ).first()
    if owner_account is None:
        account_values = {
            "family_id": secretary_account.family_id,
            "member_id": owner.pk,
            "account_name": "信诚NJ1122",
            "account_no_masked": secretary_account.account_no_masked,
            "account_type_ref_id": secretary_account.account_type_ref_id,
            "account_region_id": secretary_account.account_region_id,
            "is_active": True,
            "remark": "资产账户（不用于打新）",
            "extra_data": {},
        }
        if not bank_account_model.objects.filter(pk=50).exists():
            account_values["pk"] = 50
        owner_account = bank_account_model.objects.create(**account_values)
    else:
        owner_account.remark = "资产账户（不用于打新）"
        owner_account.is_active = True
        owner_account.save(update_fields=["remark", "is_active", "updated_at"])

    for category_name, amounts in BALANCE_SPLIT.items():
        secretary_entry = (
            balance_entry_model.objects.filter(
                account_id=secretary_account.pk,
                snapshot__snapshot_date="2026-05-31",
                asset_category__name=category_name,
                currency="HKD",
            )
            .select_related("snapshot", "asset_category")
            .first()
        )
        if secretary_entry is None:
            continue

        secretary_entry.original_amount = amounts["secretary_original"]
        secretary_entry.base_amount = amounts["secretary_base"]
        secretary_entry.save(
            update_fields=["original_amount", "base_amount", "updated_at"]
        )

        balance_entry_model.objects.update_or_create(
            snapshot_id=secretary_entry.snapshot_id,
            account_id=owner_account.pk,
            asset_category_id=secretary_entry.asset_category_id,
            currency=secretary_entry.currency,
            defaults={
                "member_id": owner.pk,
                "account_name": owner_account.account_name,
                "original_amount": amounts["owner_original"],
                "base_amount": amounts["owner_base"],
                "display_order": max(secretary_entry.display_order - 1, 0),
                "remark": "",
                "extra_data": {},
            },
        )


class Migration(migrations.Migration):
    dependencies = [
        ("ipo", "0013_merge_nj1122_account"),
    ]

    operations = [
        migrations.RunPython(
            restore_nj1122_asset_split,
            migrations.RunPython.noop,
        ),
    ]
