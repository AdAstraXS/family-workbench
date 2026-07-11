from datetime import date
from datetime import date
from decimal import Decimal

from django.db.models import Sum

from family_core.models import ExchangeRate

from .models import InvestmentCashMovement, InvestmentPosition


ZERO = Decimal("0")


def exchange_rate(source, target, on_date=None):
    source, target = source.upper(), target.upper()
    if source == target:
        return Decimal("1")
    rates = ExchangeRate.objects.filter(rate_date__lte=on_date or date.today())

    def latest(base, quote):
        item = (
            rates.filter(base_currency=base, quote_currency=quote)
            .order_by("-rate_date")
            .first()
        )
        return item.rate if item else None

    direct = latest(source, target)
    if direct is not None:
        return direct
    inverse = latest(target, source)
    if inverse:
        return Decimal("1") / inverse
    source_cny = latest(source, "CNY")
    target_cny = latest(target, "CNY")
    if source_cny and target_cny:
        return source_cny / target_cny
    return None


def convert_currency(amount, source, target, on_date=None):
    rate = exchange_rate(source, target, on_date)
    return None if rate is None else (amount or ZERO) * rate


def value_portfolio(accounts, target_currency, on_date, *, refresh_positions=False):
    accounts = list(accounts)
    positions = list(
        InvestmentPosition.objects.filter(account__in=accounts)
        .select_related("account__bank_account", "security", "security__market_snapshot")
        .order_by("account_id", "security__symbol")
    )
    missing_rates = False
    total_cash = ZERO
    cash_lines = []
    for row in (
        InvestmentCashMovement.objects.filter(
            account__in=accounts,
            movement_date__lte=on_date,
        )
        .values("account_id", "currency")
        .annotate(amount=Sum("amount"))
    ):
        rate = exchange_rate(row["currency"], target_currency, on_date)
        if rate is None:
            missing_rates = True
            continue
        amount = row["amount"] or ZERO
        converted = amount * rate
        total_cash += converted
        cash_lines.append({**row, "fx_rate": rate, "converted": converted})

    total_market_value = ZERO
    total_cost = ZERO
    changed = []
    for position in positions:
        quote = getattr(position.security, "market_snapshot", None)
        price = (
            quote.last_price
            if quote and quote.last_price is not None
            else position.current_price
        )
        rate = exchange_rate(position.security.currency, target_currency, on_date)
        position.valuation_price = price
        position.valuation_fx_rate = rate
        position.valuation_market_value_original = position.quantity * price
        position.valuation_cost_original = position.quantity * position.avg_cost
        if rate is None:
            position.valuation_market_value = None
            position.valuation_cost = None
            missing_rates = True
            continue
        position.valuation_market_value = position.valuation_market_value_original * rate
        position.valuation_cost = position.valuation_cost_original * rate
        total_market_value += position.valuation_market_value
        total_cost += position.valuation_cost
        if refresh_positions and position.current_price != price:
            position.current_price = price
            position.market_value = position.valuation_market_value_original
            position.unrealized_pnl = (
                position.market_value - position.valuation_cost_original
            )
            position.pnl_ratio = (
                position.unrealized_pnl / position.valuation_cost_original
                if position.valuation_cost_original
                else ZERO
            )
            changed.append(position)
    if changed:
        InvestmentPosition.objects.bulk_update(
            changed,
            ["current_price", "market_value", "unrealized_pnl", "pnl_ratio"],
        )

    return {
        "positions": positions,
        "cash_lines": cash_lines,
        "total_cash": total_cash,
        "total_market_value": total_market_value,
        "total_cost": total_cost,
        "total_asset": total_cash + total_market_value,
        "total_pnl": total_market_value - total_cost,
        "missing_rates": missing_rates,
    }
