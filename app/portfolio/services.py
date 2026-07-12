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
        multiplier = item.security.contract_multiplier if item.security_id else Decimal("1")
        amount = item.amount or (
            item.security.market_value_for(quantity, item.price, include_accrued=False)
            if item.security_id
            else quantity * item.price * multiplier
        )
        fee_and_tax = (item.fee or ZERO) + (item.tax or ZERO)

        if item.security_id and item.security.asset_type == item.security.TYPE_OPTION:
            if quantity <= 0:
                raise ValidationError(f"期权交易 #{item.pk} 的合约张数必须大于 0。")
            if item.trade_type not in {TradeTypeChoices.BUY, TradeTypeChoices.SELL}:
                raise ValidationError(f"期权交易 #{item.pk} 只支持买入或卖出。")
            if item.position_effect not in {
                InvestmentTransaction.EFFECT_OPEN,
                InvestmentTransaction.EFFECT_CLOSE,
            }:
                raise ValidationError(f"期权交易 #{item.pk} 必须选择开仓或平仓。")
            delta = quantity if item.trade_type == TradeTypeChoices.BUY else -quantity
            cash_change = (
                -(amount + fee_and_tax)
                if item.trade_type == TradeTypeChoices.BUY
                else amount - fee_and_tax
            )
            if item.position_effect == InvestmentTransaction.EFFECT_OPEN:
                if result.quantity and (result.quantity > 0) != (delta > 0):
                    raise ValidationError(f"期权交易 #{item.pk} 的开仓方向与当前持仓相反，请先平仓。")
                result.quantity += delta
                result.remaining_cost -= cash_change
                updates.append((item, cash_change, ZERO, ZERO, ZERO))
                continue

            if not result.quantity or (result.quantity > 0) == (delta > 0):
                raise ValidationError(f"期权交易 #{item.pk} 的平仓方向与当前持仓不匹配。")
            if quantity > abs(result.quantity):
                raise ValidationError(f"期权交易 #{item.pk} 的平仓张数超过当时持仓。")
            signed_cost = result.average_cost * quantity * (
                Decimal("1") if result.quantity > 0 else Decimal("-1")
            )
            realized_pnl = cash_change - signed_cost
            result.quantity += delta
            result.remaining_cost -= signed_cost
            if not result.quantity:
                result.remaining_cost = ZERO
            result.realized_pnl += realized_pnl
            realized_return = realized_pnl / abs(signed_cost) if signed_cost else ZERO
            updates.append(
                (item, cash_change, abs(signed_cost), realized_pnl, realized_return)
            )
            continue

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
        elif item.trade_type in {
            TradeTypeChoices.DIVIDEND,
            TradeTypeChoices.INTEREST,
        }:
            cash_change = amount - fee_and_tax
            result.realized_pnl += cash_change
            updates.append((item, cash_change, ZERO, cash_change, ZERO))
        elif item.trade_type == TradeTypeChoices.OTHER_FEE_ADJUSTMENT:
            cash_change = -(abs(amount) + fee_and_tax)
            result.realized_pnl += cash_change
            updates.append((item, cash_change, ZERO, cash_change, ZERO))
        else:
            cash_change = amount - fee_and_tax
            result.realized_pnl += cash_change
            updates.append((item, cash_change, ZERO, cash_change, ZERO))

    return result, updates


@transaction.atomic
def rebuild_position(account, security):
    transactions = list(
        InvestmentTransaction.objects.filter(
            account=account,
            security=security,
        ).select_related(
            "security__option_contract", "security__bond_detail"
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
        if item.status in {TradeStatusChoices.PARTIAL, TradeStatusChoices.COMPLETED}:
            movement_types = {
                TradeTypeChoices.BUY: CashMovementTypeChoices.BUY,
                TradeTypeChoices.IPO: CashMovementTypeChoices.BUY,
                TradeTypeChoices.SELL: CashMovementTypeChoices.SELL,
                TradeTypeChoices.DIVIDEND: CashMovementTypeChoices.DIVIDEND,
                TradeTypeChoices.INTEREST: CashMovementTypeChoices.INTEREST,
                TradeTypeChoices.OTHER_FEE_ADJUSTMENT: CashMovementTypeChoices.FEE,
                TradeTypeChoices.OTHER: CashMovementTypeChoices.ADJUSTMENT,
            }
            InvestmentCashMovement.objects.update_or_create(
                transaction=item,
                defaults={
                    "account": item.account,
                    "movement_date": item.trade_date,
                    "movement_type": movement_types[item.trade_type],
                    "currency": item.currency,
                    "amount": cash_change,
                    "source": item.source,
                    "external_id": item.external_id,
                },
            )
        else:
            InvestmentCashMovement.objects.filter(transaction=item).delete()

    position = InvestmentPosition.objects.filter(
        account=account,
        security=security,
    ).first()
    latest_trade = transactions[-1] if transactions else None
    if not position and not latest_trade:
        return None
    if not position:
        position = InvestmentPosition(account=account, security=security)

    current_price = position.current_price or (
        latest_trade.price if latest_trade else ZERO
    )
    multiplier = security.contract_multiplier
    market_value = security.market_value_for(result.quantity, current_price)
    unrealized_pnl = market_value - result.remaining_cost
    position.quantity = result.quantity
    position.avg_cost = result.average_cost / multiplier
    position.diluted_cost = result.diluted_cost / multiplier
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


@transaction.atomic
def rebuild_cash_only_transaction(item):
    if item.security_id:
        return rebuild_position(item.account, item.security)
    _, updates = calculate_transactions([item])
    _, cash_change, sell_cost, realized_pnl, realized_return = updates[0]
    InvestmentTransaction.objects.filter(pk=item.pk).update(
        cash_change=cash_change,
        sell_cost=sell_cost,
        realized_pnl=realized_pnl,
        realized_return_ratio=realized_return,
    )
    movement_types = {
        TradeTypeChoices.DIVIDEND: CashMovementTypeChoices.DIVIDEND,
        TradeTypeChoices.INTEREST: CashMovementTypeChoices.INTEREST,
        TradeTypeChoices.OTHER_FEE_ADJUSTMENT: CashMovementTypeChoices.FEE,
        TradeTypeChoices.OTHER: CashMovementTypeChoices.ADJUSTMENT,
    }
    if item.status in {TradeStatusChoices.PARTIAL, TradeStatusChoices.COMPLETED}:
        InvestmentCashMovement.objects.update_or_create(
            transaction=item,
            defaults={
                "account": item.account,
                "movement_date": item.trade_date,
                "movement_type": movement_types[item.trade_type],
                "currency": item.currency,
                "amount": cash_change,
                "source": item.source,
                "external_id": item.external_id,
            },
        )
    else:
        InvestmentCashMovement.objects.filter(transaction=item).delete()
    return item
