"""Shared classification vocabulary. Financial instrument types remain independent."""
from datetime import date
from django.core.exceptions import ValidationError
from django.db.models import Q

PRIMARY_CATEGORIES = (
    ("cash", "现金及现金等价物"), ("fixed_income", "固定收益类"),
    ("equity", "权益类"), ("derivatives", "衍生品"),
    ("alternatives", "另类投资"), ("commodities", "商品类"),
    ("insurance", "保险类"), ("liabilities", "负债"),
)
SECONDARY_CATEGORIES = (
    ("cash_balance", "现金", "cash"), ("money_market", "货币基金", "cash"),
    ("government_short", "短期国债", "fixed_income"),
    ("government_long", "中长期国债", "fixed_income"),
    ("bond_fund", "债券基金", "fixed_income"),
    ("equity_index", "股指基金", "equity"), ("equity_fund", "股票基金", "equity"),
    ("equity_stock", "股票", "equity"), ("option", "期权", "derivatives"),
    ("crypto", "虚拟货币", "alternatives"), ("gold", "黄金", "commodities"),
    ("savings_insurance", "储蓄型保险", "insurance"),
    ("credit_card", "信用卡", "liabilities"),
)
INSTRUMENTS_BY_SECONDARY = {
    "cash_balance": {"other"}, "money_market": {"etf", "fund", "other"},
    "government_short": {"bond"}, "government_long": {"bond", "etf", "fund"},
    "bond_fund": {"etf", "fund", "other"}, "equity_index": {"etf", "fund"},
    "equity_fund": {"etf", "fund"}, "equity_stock": {"stock"},
    "option": {"option"}, "crypto": {"etf", "fund", "other"},
    "gold": {"etf", "fund", "other"}, "savings_insurance": {"other"},
    "credit_card": {"other"},
}

def category_name(category):
    return category.full_name if category else "未分类"

def primary_code(category):
    return category.primary.code if category else ""

def categories_for_family(family):
    from .models import AssetCategory
    return AssetCategory.objects.filter(Q(family=family) | Q(family=None)).select_related("parent")

def seed_categories(family, model=None):
    if model is None:
        from .models import AssetCategory
        model = AssetCategory
    primary = {}
    for order, (code, name) in enumerate(PRIMARY_CATEGORIES, 1):
        primary[code], _ = model.objects.get_or_create(
            family=family, code=code,
            defaults={"name": name, "display_order": order},
        )
    for order, (code, name, parent_code) in enumerate(SECONDARY_CATEGORIES, 1):
        existing = model.objects.filter(family=family, name=name).first()
        # Dictionary installation never reclassifies historical rows implicitly.
        if existing and existing.code != code:
            raise ValidationError(f"资产类别名称冲突：{name}；请先核对旧字典。")
        model.objects.get_or_create(family=family, code=code, defaults={
            "name": name, "parent": primary[parent_code], "display_order": order,
        })
    return primary

def validate_assignment(category, *, family=None, instrument=None):
    if not category:
        return
    if family and category.family_id not in (None, family.pk):
        raise ValidationError("资产类别不属于当前家庭。")
    if category.parent_id and category.parent.family_id != category.family_id:
        raise ValidationError("一级与二级类别的家庭不一致。")
    allowed = INSTRUMENTS_BY_SECONDARY.get(category.code)
    if instrument and allowed and instrument not in allowed:
        raise ValidationError("金融品种与二级资产类别不匹配。")

def government_term_category(issue_date, maturity_date):
    if not issue_date or not maturity_date or maturity_date <= issue_date:
        return None
    # Anniversary comparison rather than 365 days also handles leap years.
    anniversary = date(issue_date.year + 1, issue_date.month, min(issue_date.day, 28)) if (
        issue_date.month == 2 and issue_date.day == 29
    ) else issue_date.replace(year=issue_date.year + 1)
    return "government_short" if maturity_date <= anniversary else "government_long"
