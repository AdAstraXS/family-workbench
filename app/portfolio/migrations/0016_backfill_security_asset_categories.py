from django.db import migrations


CATEGORY_CODES = {
    "stock": "equity",
    "etf": "fund",
    "fund": "fund",
    "bond": "fixed_income",
    "option": "derivatives",
    "crypto": "alternatives",
    "other": "alternatives",
}


def backfill_security_categories(apps, schema_editor):
    AssetCategory = apps.get_model("family_core", "AssetCategory")
    InvestmentPosition = apps.get_model("portfolio", "InvestmentPosition")
    InvestmentTransaction = apps.get_model("portfolio", "InvestmentTransaction")
    Security = apps.get_model("portfolio", "Security")

    for security in Security.objects.filter(asset_category__isnull=True):
        code = CATEGORY_CODES.get(security.asset_type)
        if not code:
            continue
        family_id = (
            InvestmentPosition.objects.filter(security_id=security.pk)
            .values_list("account__bank_account__family_id", flat=True)
            .first()
            or InvestmentTransaction.objects.filter(security_id=security.pk)
            .values_list("account__bank_account__family_id", flat=True)
            .first()
        )
        category = (
            AssetCategory.objects.filter(family_id=family_id, code=code).first()
            if family_id
            else None
        )
        if category is None:
            category = AssetCategory.objects.filter(
                family__isnull=True,
                code=code,
            ).first()
        if category is not None:
            security.asset_category_id = category.pk
            security.save(update_fields=["asset_category"])


class Migration(migrations.Migration):
    dependencies = [
        ("family_core", "0008_assetcategory_code_constraint"),
        ("portfolio", "0015_bonddetail"),
    ]

    operations = [
        migrations.RunPython(
            backfill_security_categories,
            migrations.RunPython.noop,
        ),
    ]
