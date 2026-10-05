from decimal import Decimal

from django import template

register = template.Library()


@register.filter
def macro_number(value):
    """Render exact decimal text without padding; no binary float conversion."""
    if value is None:
        return "来源缺值"
    text = format(Decimal(value), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


@register.filter
def macro_period(value, frequency):
    if not value:
        return ""
    if frequency == "月度":
        return f"{value.year}年{value.month}月"
    if frequency == "季度":
        return f"{value.year}年 第{(value.month-1)//3+1}季度"
    if frequency == "年度":
        return f"{value.year}年"
    return value.isoformat()
