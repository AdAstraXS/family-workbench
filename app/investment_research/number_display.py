"""Readable magnitudes only: never convert currencies or change stored values."""
from decimal import Decimal, InvalidOperation

CURRENCIES = {"USD": "美元", "CNY": "元", "RMB": "元", "HKD": "港元",
              "TWD": "新台币", "KRW": "韩元", "EUR": "欧元", "JPY": "日元",
              "CAD": "加元", "AUD": "澳元", "SGD": "新加坡元", "GBP": "英镑"}


def number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def money(value, currency, *, per_share=False):
    value = number(value)
    if value is None:
        return "—"
    unit = CURRENCIES.get(currency, currency) or "（币种未标注）"
    scale, prefix = Decimal(1), ""
    if not per_share:
        if abs(value) >= 100000000:
            scale, prefix = Decimal(100000000), "亿"
        elif abs(value) >= 10000:
            scale, prefix = Decimal(10000), "万"
    return f"{value / scale:,.2f} {prefix}{unit}" + ("/股" if per_share else "")
