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
def cn_market_cap(value):
    if value in (None, ""):
        return "-"
    try:
        amount = Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        return value
    trillion = Decimal("1000000000000")
    divisor, unit = (trillion, "万亿") if abs(amount) >= trillion else (Decimal("100000000"), "亿")
    amount = (amount / divisor).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{amount:,.2f} {unit}"


def _currency(value, code, places):
    if value in (None, ""):
        return "-"
    try:
        amount = Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        return value
    symbol = {"CNY": "¥", "HKD": "HK$", "USD": "US$"}.get(str(code).upper(), f"{code} ")
    return f"{symbol}{amount:,.{places}f}"


@register.filter
def currency0(value, code):
    return _currency(value, code, 0)


@register.filter
def currency2(value, code):
    return _currency(value, code, 2)


@register.filter
def signed_currency0(value, code):
    rendered = _currency(value, code, 0)
    return f"+{rendered}" if value not in (None, "") and Decimal(value) > 0 else rendered


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
