"""Read-only option position summaries from the portfolio transaction journal."""

from collections import defaultdict
from decimal import Decimal

from portfolio.models import (
    InvestmentPosition, InvestmentTransaction, Security,
    TradeStatusChoices, TradeTypeChoices,
)

from .position_evidence import participating_accounts, stock_evidence


def _purpose_from_trades(position, trades):
    """Keep only the purposes attached to opening lots that remain outstanding."""
    lots = []
    direction = 1 if position.quantity > 0 else -1
    for trade in trades:
        if trade.position_effect not in (InvestmentTransaction.EFFECT_OPEN, InvestmentTransaction.EFFECT_CLOSE):
            return "", "待归类"
        if trade.trade_type not in (TradeTypeChoices.BUY, TradeTypeChoices.SELL) or trade.quantity <= 0:
            return "", "待归类"
        signed = 1 if trade.trade_type == TradeTypeChoices.BUY else -1
        if trade.position_effect == InvestmentTransaction.EFFECT_OPEN:
            if lots and signed != lots[0][2]:
                return "", "待归类"
            lots.append([trade.quantity, trade.option_purpose, signed])
        else:
            if not lots or signed == lots[0][2]:
                return "", "待归类"
            remaining = trade.quantity
            while remaining > 0 and lots:
                used = min(remaining, lots[0][0])
                lots[0][0] -= used
                remaining -= used
                if lots[0][0] == 0:
                    lots.pop(0)
            if remaining:
                return "", "待归类"
    if not lots or lots[0][2] != direction or sum((lot[0] for lot in lots), Decimal(0)) != abs(position.quantity):
        return "", "待归类"
    purposes = {lot[1] for lot in lots}
    if "" in purposes:
        return "", "部分待归类" if len(purposes) > 1 else "待归类"
    if len(purposes) != 1:
        return "", "混合用途"
    code = purposes.pop()
    return code, dict(InvestmentTransaction.OPTION_PURPOSE_CHOICES).get(code, "待归类") if code else "待归类"


def option_position_rows(family, stocks=None):
    accounts = participating_accounts(family)
    account_ids = [account.pk for account in accounts.values()]
    stocks = stock_evidence(family) if stocks is None else stocks
    positions = list(InvestmentPosition.objects.filter(
        account_id__in=account_ids, security__asset_type=Security.TYPE_OPTION,
        security__option_contract__isnull=False,
        security__option_contract__underlying__market__iexact="US",
    ).exclude(quantity=0).select_related(
        "account__bank_account", "security__option_contract__underlying",
    ).order_by("account__bank_account__account_name", "security__option_contract__expiration_date", "pk"))
    transactions = InvestmentTransaction.objects.filter(
        account_id__in=account_ids,
        security_id__in=[position.security_id for position in positions],
        status__in=[TradeStatusChoices.COMPLETED, TradeStatusChoices.PARTIAL],
    ).order_by("trade_date", "created_at", "pk")
    by_position = defaultdict(list)
    for trade in transactions:
        by_position[(trade.account_id, trade.security_id)].append(trade)
    rows = []
    for position in positions:
        contract = position.security.option_contract
        purpose, purpose_label = _purpose_from_trades(position, by_position[(position.account_id, position.security_id)])
        multiplier = Decimal(contract.multiplier)
        contracts = abs(position.quantity)
        stock = stocks.get((position.account_id, contract.underlying.symbol.upper()))
        rows.append({
            "position": position, "contract": contract,
            "account_id": position.account_id, "account": position.account.account_name,
            "symbol": contract.underlying.symbol.upper(), "name": contract.underlying.name,
            "side": "多头" if position.quantity > 0 else "空头",
            "contracts": contracts, "purpose": purpose, "purpose_label": purpose_label,
            "open_per_contract": abs(position.avg_cost) * multiplier if position.avg_cost else None,
            "mark_per_contract": position.current_price * multiplier if position.current_price_as_of else None,
            "mark_as_of": position.current_price_as_of,
            "stock_shares": stock["shares"] if stock else Decimal(0),
            "covered_shares": contracts * multiplier,
            "protection_covered": bool(stock and stock["shares"] >= contracts * multiplier),
            "stock_cost": stock["cost"] if stock else None,
            "stock_as_of": stock["as_of"] if stock else None,
        })
    return rows
