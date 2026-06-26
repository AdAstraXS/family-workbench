from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re

from django import template


register = template.Library()


@register.filter
def thousands(value):
    if value in (None, ""):
        return "-"
    try:
        amount = Decimal(value).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError):
        return value
    return f"{amount:,}"


@register.filter
def money2(value):
    if value in (None, ""):
        return "-"
    try:
        amount = Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError):
        return value
    return f"{amount:,.2f}"


@register.filter
def lots(value):
    if value in (None, ""):
        return "-"
    try:
        amount = Decimal(value).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError):
        return value
    return f"{amount:,}"


@register.filter
def company_lines(value):
    if not value:
        return "-"
    text = str(value)
    text = re.sub(r"[、,，;；]\s*", "\n", text)
    return text
