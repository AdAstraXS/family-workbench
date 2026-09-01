"""Idempotently project completed portfolio transactions into wheel lifecycle records."""

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max, Q, Sum
from django.utils import timezone

from portfolio.models import InvestmentTransaction, OptionContract, TradeStatusChoices, TradeTypeChoices

from .models import (
    CycleStatus, LegStatus, Strategy, WheelCollateralReservation,
    WheelCycle, WheelLeg, WheelTransactionLink,
)

MONEY_STEP = Decimal("0.0001")


class WheelLifecycleError(ValueError):
    pass


def eligible_unlinked_transactions(*, family, account_ids):
    return InvestmentTransaction.objects.filter(
        account_id__in=account_ids,
        account__bank_account__family=family,
        security__asset_type="option",
        status__in=[TradeStatusChoices.COMPLETED, TradeStatusChoices.PARTIAL],
    ).filter(
        Q(trade_type=TradeTypeChoices.SELL, position_effect=InvestmentTransaction.EFFECT_OPEN)
        | Q(trade_type=TradeTypeChoices.BUY, position_effect=InvestmentTransaction.EFFECT_CLOSE)
    ).exclude(wheel_links__isnull=False).select_related(
        "account", "security__option_contract__underlying"
    ).order_by("trade_date", "pk")


def _active_cycle(transaction_item, contract):
    return WheelCycle.objects.select_for_update().filter(
        family=transaction_item.account.family,
        account=transaction_item.account,
        underlying=contract.underlying,
        status__in=[CycleStatus.OPEN, CycleStatus.PAUSED],
    ).first()


def _open_leg(cycle, contract):
    return WheelLeg.objects.select_for_update().filter(
        cycle=cycle,
        option_contract=contract,
        status=LegStatus.OPEN,
    ).order_by("sequence").first()


def _next_sequence(cycle):
    return (cycle.legs.aggregate(value=Max("sequence"))["value"] or 0) + 1


def _release_leg_collateral(leg, moment):
    reservation = leg.collateral_reservations.select_for_update().filter(released_at__isnull=True).first()
    if reservation:
        reservation.released_at = moment
        reservation.save(update_fields=["released_at", "updated_at"])


def _reduce_leg_collateral(leg, allocated, original_open_count, moment):
    reservation = leg.collateral_reservations.select_for_update().filter(released_at__isnull=True).first()
    if reservation is None:
        raise WheelLifecycleError("活动分段缺少担保预留记录。")
    if allocated == original_open_count:
        reservation.released_at = moment
        reservation.save(update_fields=["released_at", "updated_at"])
        return
    ratio = Decimal(original_open_count - allocated) / Decimal(original_open_count)
    if reservation.kind == WheelCollateralReservation.CASH:
        reservation.cash_amount = (reservation.cash_amount * ratio).quantize(MONEY_STEP)
        reservation.save(update_fields=["cash_amount", "updated_at"])
    else:
        reservation.share_quantity = reservation.share_quantity * ratio
        reservation.save(update_fields=["share_quantity", "updated_at"])


@transaction.atomic
def sync_transaction(transaction_item):
    transaction_item = InvestmentTransaction.objects.select_for_update(of=("self",)).select_related(
        "account__bank_account__family", "security__option_contract__underlying"
    ).get(pk=transaction_item.pk)
    existing_link = transaction_item.wheel_links.select_related("leg").first()
    if existing_link:
        return existing_link.leg, False
    try:
        contract = transaction_item.security.option_contract
    except Exception:
        raise WheelLifecycleError("交易没有完整的期权合约资料。") from None
    if (
        transaction_item.quantity <= 0
        or transaction_item.quantity != transaction_item.quantity.to_integral_value()
    ):
        raise WheelLifecycleError("期权交易张数必须是正整数。")
    cycle = _active_cycle(transaction_item, contract)
    is_short_open = (
        transaction_item.trade_type == TradeTypeChoices.SELL
        and transaction_item.position_effect == InvestmentTransaction.EFFECT_OPEN
    )
    is_short_close = (
        transaction_item.trade_type == TradeTypeChoices.BUY
        and transaction_item.position_effect == InvestmentTransaction.EFFECT_CLOSE
    )
    if not (is_short_open or is_short_close):
        raise WheelLifecycleError("M1 只关联卖方开仓及其买入平仓/指派流水。")

    now = timezone.now()
    action = transaction_item.extra_data.get("option_action") if isinstance(transaction_item.extra_data, dict) else None
    if is_short_open:
        if cycle is None:
            if contract.option_type != OptionContract.PUT:
                raise WheelLifecycleError("没有活动周期时不能从 Covered Call 开始。")
            cycle = WheelCycle.objects.create(
                family=transaction_item.account.family, account=transaction_item.account,
                underlying=contract.underlying, opened_on=transaction_item.trade_date,
            )
        previous = cycle.legs.filter(
            status__in=[LegStatus.CLOSED, LegStatus.ASSIGNED, LegStatus.EXPIRED]
        ).order_by("-sequence").first()
        base_strategy = Strategy.SELL_PUT if contract.option_type == OptionContract.PUT else Strategy.COVERED_CALL
        strategy = Strategy.ROLL if previous and previous.closed_at and previous.closed_at.date() == transaction_item.trade_date else base_strategy
        leg = WheelLeg.objects.create(
            cycle=cycle, parent_leg=previous if strategy == Strategy.ROLL else None,
            sequence=_next_sequence(cycle), strategy=strategy, status=LegStatus.OPEN,
            option_contract=contract,
            expiration=contract.expiration_date, strike=contract.strike_price,
            contract_count=int(transaction_item.quantity),
            open_contract_count=int(transaction_item.quantity),
            premium_total=(transaction_item.amount - transaction_item.fee - transaction_item.tax),
            opened_at=now,
        )
        if contract.option_type == OptionContract.PUT:
            WheelCollateralReservation.objects.create(
                leg=leg, account=cycle.account, kind=WheelCollateralReservation.CASH,
                currency="USD",
                cash_amount=(contract.strike_price * Decimal(contract.multiplier) * transaction_item.quantity).quantize(MONEY_STEP),
            )
        else:
            if cycle.assigned_cost_basis is None:
                raise WheelLifecycleError("Covered Call 缺少本周期的指派或买入成本。")
            if contract.strike_price < cycle.assigned_cost_basis:
                raise WheelLifecycleError("Covered Call 行权价低于本周期指派或买入成本。")
            already_reserved = WheelCollateralReservation.objects.select_for_update().filter(
                account=cycle.account, kind=WheelCollateralReservation.SHARES,
                released_at__isnull=True, leg__cycle=cycle,
            ).aggregate(value=Sum("share_quantity"))["value"] or Decimal("0")
            required_shares = Decimal(contract.multiplier) * transaction_item.quantity
            if already_reserved + required_shares > cycle.assigned_share_quantity:
                raise WheelLifecycleError("Covered Call 超过本周期可覆盖的正股数量。")
            WheelCollateralReservation.objects.create(
                leg=leg, account=cycle.account, kind=WheelCollateralReservation.SHARES,
                share_quantity=required_shares,
            )
        WheelTransactionLink.objects.create(
            leg=leg, transaction=transaction_item, role="open",
            linked_quantity=transaction_item.quantity,
        )
        return leg, True

    if cycle is None:
        raise WheelLifecycleError("找不到与平仓流水对应的活动车轮周期。")
    remaining = int(transaction_item.quantity)
    linked_legs = []
    while remaining:
        leg = _open_leg(cycle, contract)
        if leg is None:
            raise WheelLifecycleError("平仓张数超过可匹配的活动分段。")
        original_open_count = leg.open_contract_count
        allocated = min(remaining, original_open_count)
        leg.open_contract_count -= allocated
        allocated_cash = transaction_item.cash_change * Decimal(allocated) / transaction_item.quantity
        leg.premium_total += allocated_cash
        if leg.open_contract_count == 0:
            leg.closed_at = now
            if action == "assignment":
                leg.status = LegStatus.ASSIGNED
            elif action == "expire":
                leg.status = LegStatus.EXPIRED
            else:
                leg.status = LegStatus.CLOSED
        leg.save(update_fields=["status", "closed_at", "premium_total", "open_contract_count", "updated_at"])
        _reduce_leg_collateral(leg, allocated, original_open_count, now)
        WheelTransactionLink.objects.create(
            leg=leg, transaction=transaction_item, role=action or "close",
            linked_quantity=Decimal(allocated),
        )
        linked_legs.append((leg, allocated))
        remaining -= allocated
    if action == "assignment":
        assigned_shares = Decimal(contract.multiplier) * transaction_item.quantity
        if contract.option_type == OptionContract.PUT:
            cycle.assigned_cost_basis = max(cycle.assigned_cost_basis or Decimal("0"), contract.strike_price)
            cycle.assigned_share_quantity += assigned_shares
        else:
            if assigned_shares > cycle.assigned_share_quantity:
                raise WheelLifecycleError("Call 指派股数超过本周期持有股数。")
            cycle.assigned_share_quantity -= assigned_shares
            if cycle.assigned_share_quantity == 0 and not cycle.legs.filter(status=LegStatus.OPEN).exists():
                cycle.status = CycleStatus.CLOSED
                cycle.closed_on = transaction_item.trade_date
        cycle.save(update_fields=["assigned_cost_basis", "assigned_share_quantity", "status", "closed_on", "updated_at"])
    underlying_id = transaction_item.extra_data.get("underlying_transaction_id") if isinstance(transaction_item.extra_data, dict) else None
    if underlying_id:
        underlying_transaction = InvestmentTransaction.objects.get(pk=underlying_id, account=cycle.account)
        for linked_leg, allocated in linked_legs:
            WheelTransactionLink.objects.get_or_create(
                transaction=underlying_transaction, leg=linked_leg, role="assignment_underlying",
                defaults={"linked_quantity": Decimal(allocated)},
            )
    return linked_legs[0][0], True


@transaction.atomic
def sync_transactions(*, family, account_ids):
    results = []
    for item in eligible_unlinked_transactions(family=family, account_ids=account_ids):
        try:
            results.append(sync_transaction(item))
        except (ValidationError, WheelLifecycleError) as exc:
            raise WheelLifecycleError(f"交易 #{item.pk} 无法关联：{exc}") from None
    return results
