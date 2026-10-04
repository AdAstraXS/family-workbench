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
