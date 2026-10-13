from decimal import Decimal
from django import template

register = template.Library()


@register.filter
def decimal_text(value):
    if value is None:
        return "—"
    text = format(Decimal(value), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text
