from decimal import Decimal, ROUND_HALF_UP

from django.core.exceptions import ValidationError

from portfolio.valuation import resolve_exchange_rate


class MissingCashflowRate(ValueError):
    pass


def cashflow_amount(record):
    """CNY value at the transaction date, or period end for monthly records."""
    on_date = record.period_end or getattr(record, "income_date", None) or record.expense_date
    rate = resolve_exchange_rate(record.currency, "CNY", on_date).rate
    if rate is None:
        kind = "收入" if hasattr(record, "income_date") else "支出"
        raise MissingCashflowRate(
            f"{kind}记录 #{record.pk}（{on_date}，{record.amount} {record.currency}）缺少当日或此前可用的人民币汇率。"
        )
    return ((record.amount or Decimal("0")) * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def calculate_base_amount(snapshot, currency, original_amount):
    amount = original_amount or Decimal("0")
    if currency == snapshot.base_currency or not amount:
        return amount
    rate = {"USD": snapshot.usd_to_base, "HKD": snapshot.hkd_to_base}.get(currency)
    if rate is None or rate <= 0:
        if snapshot.is_draft:
            return Decimal("0")
        raise ValidationError(f"正式资产快照包含 {currency} 余额，请填写大于 0 的对应汇率。")
    return amount * rate
