from dataclasses import dataclass
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction

from .models import (
    CashMovementTypeChoices,
    InvestmentCashMovement,
    InvestmentPosition,
    InvestmentTransaction,
    TradeStatusChoices,
    TradeTypeChoices,
)


ZERO = Decimal("0")


@dataclass
class PositionCalculation:
    quantity: Decimal = ZERO
    remaining_cost: Decimal = ZERO
    realized_pnl: Decimal = ZERO

    @property
    def average_cost(self):
        return self.remaining_cost / self.quantity if self.quantity else ZERO

    @property
    def diluted_cost(self):
        return (
            (self.remaining_cost - self.realized_pnl) / self.quantity
            if self.quantity
            else ZERO
        )


def calculate_transactions(transactions):
    result = PositionCalculation()
    updates = []
    for item in transactions:
        if item.status not in {
            TradeStatusChoices.PARTIAL,
            TradeStatusChoices.COMPLETED,
        }:
            updates.append((item, ZERO, ZERO, ZERO, ZERO))
            continue
        quantity = item.quantity or ZERO
        amount = item.amount or quantity * item.price
        fee_and_tax = (item.fee or ZERO) + (item.tax or ZERO)

        if item.trade_type in {TradeTypeChoices.BUY, TradeTypeChoices.IPO}:
            if quantity <= 0:
                raise ValidationError(f"买入交易 #{item.pk} 的数量必须大于 0。")
            result.quantity += quantity
            result.remaining_cost += amount + fee_and_tax
            updates.append((item, -(amount + fee_and_tax), ZERO, ZERO, ZERO))
        elif item.trade_type == TradeTypeChoices.SELL:
            if quantity <= 0:
                raise ValidationError(f"卖出交易 #{item.pk} 的数量必须大于 0。")
            if quantity > result.quantity:
                raise ValidationError(f"卖出交易 #{item.pk} 的数量超过当时持仓。")
            sell_cost = result.average_cost * quantity
            realized_pnl = amount - fee_and_tax - sell_cost
            result.quantity -= quantity
            result.remaining_cost -= sell_cost
            result.realized_pnl += realized_pnl
            realized_return = realized_pnl / sell_cost if sell_cost else ZERO
            updates.append(
                (item, amount - fee_and_tax, sell_cost, realized_pnl, realized_return)
            )
        else:
            updates.append((item, ZERO, ZERO, ZERO, ZERO))

    return result, updates


@transaction.atomic
def rebuild_position(account, security):
    transactions = list(
        InvestmentTransaction.objects.filter(
            account=account,
            security=security,
        ).order_by("trade_date", "created_at", "pk")
    )
    result, updates = calculate_transactions(transactions)
    for item, cash_change, sell_cost, realized_pnl, realized_return in updates:
        InvestmentTransaction.objects.filter(pk=item.pk).update(
            cash_change=cash_change,
            sell_cost=sell_cost,
            realized_pnl=realized_pnl,
            realized_return_ratio=realized_return,
        )
        if (
            item.trade_type
            in {TradeTypeChoices.BUY, TradeTypeChoices.IPO, TradeTypeChoices.SELL}
            and item.status
            in {TradeStatusChoices.PARTIAL, TradeStatusChoices.COMPLETED}
        ):
            InvestmentCashMovement.objects.update_or_create(
                transaction=item,
                defaults={
                    "account": item.account,
                    "movement_date": item.trade_date,
                    "movement_type": (
                        CashMovementTypeChoices.BUY
                        if item.trade_type
                        in {TradeTypeChoices.BUY, TradeTypeChoices.IPO}
                        else CashMovementTypeChoices.SELL
                    ),
                    "currency": item.currency,
                    "amount": cash_change,
                    "source": item.source,
                    "external_id": item.external_id,
                },
            )
        else:
            InvestmentCashMovement.objects.filter(transaction=item).delete()

    position = (
        InvestmentPosition.objects.filter(account=account, security=security)
        .order_by("-position_date", "-updated_at", "-pk")
        .first()
    )
    latest_trade = transactions[-1] if transactions else None
    if not position and not latest_trade:
        return None
    if not position:
        position = InvestmentPosition(account=account, security=security)

    current_price = position.current_price or (
        latest_trade.price if latest_trade else ZERO
    )
    market_value = result.quantity * current_price
    unrealized_pnl = market_value - result.remaining_cost
    position.quantity = result.quantity
    position.avg_cost = result.average_cost
    position.diluted_cost = result.diluted_cost
    position.current_price = current_price
    position.market_value = market_value
    position.unrealized_pnl = unrealized_pnl
    position.realized_pnl = result.realized_pnl
    position.pnl_ratio = (
        unrealized_pnl / result.remaining_cost if result.remaining_cost else ZERO
    )
    position.position_date = latest_trade.trade_date if latest_trade else position.position_date
    position.save()
    return position
