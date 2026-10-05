from django.db import migrations

PRIMARY = (
    ('cash', '现金及现金等价物'), ('fixed_income', '固定收益类'), ('equity', '权益类'),
    ('derivatives', '衍生品'), ('alternatives', '另类投资'), ('commodities', '商品类'),
    ('insurance', '保险类'), ('liabilities', '负债'),
)
SECONDARY = (
    ('cash_balance', '现金', 'cash'), ('money_market', '货币基金', 'cash'),
    ('government_short', '短期国债', 'fixed_income'), ('government_long', '中长期国债', 'fixed_income'),
    ('bond_fund', '债券基金', 'fixed_income'), ('equity_index', '股指基金', 'equity'),
    ('equity_fund', '股票基金', 'equity'), ('equity_stock', '股票', 'equity'),
    ('option', '期权', 'derivatives'), ('crypto', '虚拟货币', 'alternatives'),
    ('gold', '黄金', 'commodities'), ('savings_insurance', '储蓄型保险', 'insurance'),
    ('credit_card', '信用卡', 'liabilities'),
)

def install_dictionary(apps, schema_editor):
    Category = apps.get_model('family_core', 'AssetCategory')
    Family = apps.get_model('family_core', 'Family')
    alias = schema_editor.connection.alias
    categories = Category.objects.using(alias)
    families = list(Family.objects.using(alias).values_list('pk', flat=True))
    if categories.filter(family_id=None).exists():
        families.append(None)
    for family_id in families:
        roots = {}
        for order, (code, name) in enumerate(PRIMARY, 1):
            roots[code], _ = categories.get_or_create(family_id=family_id, code=code, defaults={'name': name, 'display_order': order})
        for order, (code, name, parent) in enumerate(SECONDARY, 1):
            conflict = categories.filter(family_id=family_id, name=name).exclude(code=code).exists()
            if conflict:
                raise RuntimeError(f'资产类别名称冲突：{name}；请先核对旧字典，迁移未调整历史分类。')
            categories.get_or_create(family_id=family_id, code=code, defaults={'name': name, 'parent': roots[parent], 'display_order': order})

class Migration(migrations.Migration):
    dependencies = [
        ('family_core', '0009_assetcategory_parent_assetclassificationaudit'),
        ('portfolio', '0032_bonddetail_original_issue_date_and_more'),
    ]
    # Old categories and every financial record remain untouched. Dictionary
    # rows stay on reverse so historical FK references can never be removed.
    operations = [migrations.RunPython(install_dictionary, migrations.RunPython.noop)]
