from django.db import migrations


STANDARD_CATEGORIES = [
    ("cash", "现金及现金等价物", 1),
    ("equity", "权益类", 2),
    ("fixed_income", "固定收益类", 3),
    ("fund", "基金类", 4),
    ("derivatives", "衍生品", 5),
    ("commodities", "商品类", 6),
    ("alternatives", "另类投资", 7),
]

LEGACY_CODE_MAP = {
    "现金": "cash",
    "打新-现金": "cash",
    "股票": "equity",
    "债券": "fixed_income",
    "美债": "fixed_income",
    "低风险理财": "fixed_income",
    "基金": "fund",
    "指数基金": "fund",
    "股指基金": "fund",
    "期权": "derivatives",
    "黄金": "commodities",
    "虚拟货币": "alternatives",
    "套利": "alternatives",
}


def standardize_asset_categories(apps, schema_editor):
    Family = apps.get_model("family_core", "Family")
    AssetCategory = apps.get_model("family_core", "AssetCategory")
    Security = apps.get_model("portfolio", "Security")
    InvestmentTransaction = apps.get_model("portfolio", "InvestmentTransaction")
    AssetBalanceEntry = apps.get_model("ledger", "AssetBalanceEntry")

    for family in Family.objects.order_by("pk"):
        targets = {}
        for code, name, order in STANDARD_CATEGORIES:
            target, _ = AssetCategory.objects.update_or_create(
                family=family,
                code=code,
                defaults={"name": name, "display_order": order, "is_active": True},
            )
            targets[code] = target
        legacy = list(
            AssetCategory.objects.filter(
                family=family, name__in=LEGACY_CODE_MAP
            ).exclude(pk__in=[item.pk for item in targets.values()])
        )
        for old in legacy:
            target = targets[LEGACY_CODE_MAP[old.name]]
            Security.objects.filter(asset_category_id=old.pk).update(asset_category_id=target.pk)
            InvestmentTransaction.objects.filter(asset_category_id=old.pk).update(asset_category_id=target.pk)
            AssetBalanceEntry.objects.filter(asset_category_id=old.pk).update(asset_category_id=target.pk)
            old.delete()

    for item in AssetCategory.objects.filter(code="").order_by("pk"):
        item.code = f"legacy-{item.pk}"
        item.save(update_fields=["code"])


class Migration(migrations.Migration):
    dependencies = [
        ("family_core", "0006_assetcategory_code_and_standard_categories"),
    ]

    operations = [
        migrations.RunPython(
            standardize_asset_categories,
            migrations.RunPython.noop,
        ),
    ]
