"""Read-only stock and short-option evidence from recorded portfolio facts."""

from decimal import Decimal

from portfolio.models import (
    InvestmentPosition, InvestmentTransaction, OptionContract, Security,
    TradeStatusChoices, TradeTypeChoices,
)

from .views import PARTICIPATING_ACCOUNTS, _select_participating_account


def participating_accounts(family):
    from portfolio.models import InvestmentAccount

    groups = {name: [] for name in PARTICIPATING_ACCOUNTS}
    for account in InvestmentAccount.objects.filter(
        bank_account__family=family,
        bank_account__is_active=True,
        bank_account__account_name__in=PARTICIPATING_ACCOUNTS,
    ).select_related("bank_account", "bank_account__member"):
        groups[account.account_name].append(account)
    selected = {}
    for name, matches in groups.items():
        account, ambiguous = _select_participating_account(matches)
        if not ambiguous and account is not None:
            selected[name] = account
    return selected


def stock_evidence(family, symbols=None):
    """Return stock coverage per account; ambiguous lots stay explicitly unknown."""
    accounts = participating_accounts(family)
    account_ids = [account.pk for account in accounts.values() if account is not None]
    positions = InvestmentPosition.objects.filter(
        account_id__in=account_ids, security__market__iexact="US",
        security__asset_type=Security.TYPE_STOCK, quantity__gt=0,
    ).select_related("security", "account__bank_account")
    if symbols is not None:
        positions = positions.filter(security__symbol__in=symbols)
    calls = InvestmentPosition.objects.filter(
        account_id__in=account_ids, security__asset_type=Security.TYPE_OPTION,
        security__option_contract__option_type=OptionContract.CALL,
        quantity__lt=0,
    ).select_related("security__option_contract")
    occupied = {}
    for position in calls:
        contract = position.security.option_contract
        key = (position.account_id, contract.underlying_id)
        occupied[key] = occupied.get(key, Decimal(0)) + abs(position.quantity) * contract.multiplier
    result = {}
    for position in positions:
        trades = list(InvestmentTransaction.objects.filter(
            account=position.account, security=position.security,
            status__in=[TradeStatusChoices.COMPLETED, TradeStatusChoices.PARTIAL],
            trade_type__in=[TradeTypeChoices.BUY, TradeTypeChoices.SELL],
        ).order_by("trade_date", "created_at", "pk"))
        buys = [trade for trade in trades if trade.trade_type == TradeTypeChoices.BUY]
        sold = any(trade.trade_type == TradeTypeChoices.SELL for trade in trades)
        lots = None
        if buys and not sold and sum((trade.quantity for trade in buys), Decimal(0)) == position.quantity:
            lots = [{"date": trade.trade_date, "shares": trade.quantity,
                     "cost": (trade.amount + trade.fee + trade.tax) / trade.quantity
                     if trade.quantity > 0 else None, "transaction_id": trade.pk,
                     "assignment": isinstance(trade.extra_data, dict)
                     and trade.extra_data.get("option_action") == "assignment"}
                    for trade in buys]
        used = occupied.get((position.account_id, position.security_id), Decimal(0))
        free = max(position.quantity - used, Decimal(0))
        result[(position.account_id, position.security.symbol.upper())] = {
            "account": position.account.account_name, "account_id": position.account_id,
            "symbol": position.security.symbol.upper(), "name": position.security.name,
            "shares": position.quantity, "occupied_shares": used, "free_shares": free,
            "available_contracts": int(free // 100), "cost": position.avg_cost if position.avg_cost > 0 else None,
            "cost_lots": lots, "as_of": position.position_date,
        }
    return result
