from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, time
from decimal import Decimal

from django.utils import timezone

from ledger.models import AssetBalanceEntry, AssetBalanceSnapshot

from .models import (
    InvestmentAccount,
    InvestmentCashMovement,
    InvestmentPosition,
    InvestmentTransaction,
    PriceSourceChoices,
    PricingStatusChoices,
    SecurityPriceRecord,
)
from .services import calculate_transactions
from .valuation import exchange_rate


ZERO = Decimal("0")


def account_ids_as_of(family, on_date, *, include_ledger=True):
    base = InvestmentAccount.objects.filter(bank_account__family=family)
    account_ids = set(
        base.filter(transactions__trade_date__lte=on_date).values_list("pk", flat=True)
    )
    account_ids.update(
        base.filter(cash_movements__movement_date__lte=on_date).values_list(
            "pk", flat=True
        )
    )
    account_ids.update(
        base.filter(positions__position_date__lte=on_date).values_list("pk", flat=True)
    )
    if include_ledger:
        account_ids.update(
            AssetBalanceEntry.objects.filter(
                snapshot__family=family,
                snapshot__snapshot_date=on_date,
                account__investment_profile__isnull=False,
            ).values_list("account__investment_profile__pk", flat=True)
        )
    return account_ids


@dataclass
class HistoricalPositionValue:
    account: InvestmentAccount
    security: object
    quantity: Decimal
    cost_original: Decimal
    price: Decimal | None = None
    price_as_of: object | None = None
    price_source: str = ""
    pricing_status: str = PricingStatusChoices.MISSING
    fx_rate: Decimal | None = None
    market_value_original: Decimal | None = None
    market_value: Decimal | None = None
    cost: Decimal | None = None


def snapshot_exchange_rate(source, target, on_date, ledger_snapshot=None):
    source = source.upper()
    target = target.upper()
    if source == target:
        return Decimal("1")
    if ledger_snapshot and target == ledger_snapshot.base_currency.upper():
        if source == "USD":
            return ledger_snapshot.usd_to_base or None
        if source == "HKD":
            return ledger_snapshot.hkd_to_base or None
    return exchange_rate(source, target, on_date)


def _historical_states(accounts, on_date, exclude_movement_ids=None):
    accounts = list(accounts)
    transactions = list(
        InvestmentTransaction.objects.filter(
            account__in=accounts,
            trade_date__lte=on_date,
        )
        .select_related(
            "account__bank_account__member",
            "security__market_snapshot",
            "security__option_contract",
            "security__bond_detail",
        )
        .order_by("trade_date", "created_at", "pk")
    )
    grouped = defaultdict(list)
    cash_only = defaultdict(list)
    for item in transactions:
        if item.security_id:
            grouped[(item.account_id, item.security_id)].append(item)
        else:
            cash_only[item.account_id].append(item)

    states = {
        account.pk: {
            "account": account,
            "cash": defaultdict(Decimal),
            "positions": [],
            "errors": [],
        }
        for account in accounts
    }
    for (account_id, _security_id), items in grouped.items():
        try:
            result, updates = calculate_transactions(items)
        except Exception as exc:
            states[account_id]["errors"].append(str(exc))
            continue
        for item, cash_change, *_unused in updates:
            states[account_id]["cash"][item.currency.upper()] += cash_change
        if result.quantity:
            states[account_id]["positions"].append(
                HistoricalPositionValue(
                    account=states[account_id]["account"],
                    security=items[-1].security,
                    quantity=result.quantity,
                    cost_original=result.remaining_cost,
                )
            )

    transaction_keys = set(grouped)
    manual_positions = (
        InvestmentPosition.objects.filter(
            account__in=accounts,
            position_date__lte=on_date,
        )
        .exclude(quantity=0)
        .select_related(
            "account__bank_account__member",
            "security__market_snapshot",
            "security__option_contract",
            "security__bond_detail",
        )
    )
    for position in manual_positions:
        if (position.account_id, position.security_id) in transaction_keys:
            continue
        states[position.account_id]["positions"].append(
            HistoricalPositionValue(
                account=states[position.account_id]["account"],
                security=position.security,
                quantity=position.quantity,
                cost_original=(
                    position.quantity
                    * position.avg_cost
                    * position.security.contract_multiplier
                ),
            )
        )

    for account_id, items in cash_only.items():
        try:
            _result, updates = calculate_transactions(items)
        except Exception as exc:
            states[account_id]["errors"].append(str(exc))
            continue
        for item, cash_change, *_unused in updates:
            states[account_id]["cash"][item.currency.upper()] += cash_change

    movements = InvestmentCashMovement.objects.filter(
        account__in=accounts,
        transaction=None,
        movement_date__lte=on_date,
    )
    if exclude_movement_ids:
        movements = movements.exclude(pk__in=exclude_movement_ids)
    for item in movements.order_by("movement_date", "created_at", "pk"):
        states[item.account_id]["cash"][item.currency.upper()] += item.amount
    return states


def _price_records(positions, on_date):
    security_ids = {item.security.pk for item in positions}
    if not security_ids:
        return {}
    cutoff = timezone.make_aware(datetime.combine(on_date, time.max))
    records = {}
    for item in SecurityPriceRecord.objects.filter(
        security_id__in=security_ids,
        price_as_of__lte=cutoff,
    ).order_by("security_id", "-price_as_of", "-pk"):
        records.setdefault(item.security_id, item)
    return records


def _transaction_prices(positions, on_date):
    security_ids = {item.security.pk for item in positions}
    result = {}
    for item in InvestmentTransaction.objects.filter(
        security_id__in=security_ids,
        trade_date=on_date,
        price__gt=0,
    ).order_by("created_at", "pk"):
        result[item.security_id] = item.price
    return result


def _latest_transaction_prices(positions, on_date):
    security_ids = {item.security.pk for item in positions}
    result = {}
    for item in InvestmentTransaction.objects.filter(
        security_id__in=security_ids,
        trade_date__lte=on_date,
        price__gt=0,
    ).order_by("security_id", "-trade_date", "-created_at", "-pk"):
        result.setdefault(item.security_id, (item.price, item.trade_date))
    return result


def _apply_prices(positions, on_date):
    records = _price_records(positions, on_date)
    transaction_prices = _transaction_prices(positions, on_date)
    latest_transaction_prices = _latest_transaction_prices(positions, on_date)
    for position in positions:
        security = position.security
        record = records.get(security.pk)
        snapshot = getattr(security, "market_snapshot", None)
        if record:
            position.price = record.price
            position.price_as_of = record.price_as_of
            position.price_source = record.source
            position.pricing_status = (
                PricingStatusChoices.MANUAL
                if record.source == PriceSourceChoices.MANUAL
                else PricingStatusChoices.LEGACY
                if record.source == PriceSourceChoices.LEGACY
                else PricingStatusChoices.FRESH
            )
        elif security.pk in transaction_prices:
            position.price = transaction_prices[security.pk]
            position.price_as_of = on_date
            position.price_source = "transaction"
            position.pricing_status = PricingStatusChoices.LEGACY
        elif (
            security.asset_type == security.TYPE_BOND
            and security.pk in latest_transaction_prices
        ):
            position.price, position.price_as_of = latest_transaction_prices[
                security.pk
            ]
            position.price_source = "transaction"
            position.pricing_status = PricingStatusChoices.LEGACY
        elif snapshot and snapshot.last_price is not None and (
            not snapshot.price_as_of or snapshot.price_as_of.date() <= on_date
        ):
            position.price = snapshot.last_price
            position.price_as_of = snapshot.price_as_of or snapshot.fetched_at
            position.price_source = snapshot.price_source
            position.pricing_status = PricingStatusChoices.LEGACY

        price_date = (
            position.price_as_of.date()
            if hasattr(position.price_as_of, "date")
            else position.price_as_of
        )
        if position.price is not None and price_date and price_date < on_date:
            position.pricing_status = PricingStatusChoices.STALE
        option = getattr(security, "option_contract", None)
        if option and option.expiration_date < on_date and position.quantity:
            position.pricing_status = PricingStatusChoices.EXPIRED_UNRESOLVED


def value_historical_portfolio(
    accounts,
    target_currency,
    on_date,
    *,
    ledger_snapshot: AssetBalanceSnapshot | None = None,
    exclude_movement_ids=None,
):
    accounts = list(accounts)
    states = _historical_states(accounts, on_date, exclude_movement_ids)
    positions = [
        position
        for state in states.values()
        for position in state["positions"]
    ]
    _apply_prices(positions, on_date)

    total_cash = ZERO
    cash_lines = []
    missing_rates = []
    for state in states.values():
        for currency, amount in state["cash"].items():
            rate = snapshot_exchange_rate(
                currency,
                target_currency,
                on_date,
                ledger_snapshot,
            )
            if rate is None:
                missing_rates.append(
                    {"account_id": state["account"].pk, "currency": currency}
                )
                continue
            converted = amount * rate
            total_cash += converted
            cash_lines.append(
                {
                    "account_id": state["account"].pk,
                    "currency": currency,
                    "amount": amount,
                    "fx_rate": rate,
                    "converted": converted,
                }
            )

    total_market_value = ZERO
    total_cost = ZERO
    missing_prices = []
    stale_prices = []
    for position in positions:
        security = position.security
        if position.price is None:
            missing_prices.append(
                {
                    "account_id": position.account.pk,
                    "security_id": security.pk,
                    "security": f"{security.market}:{security.symbol}",
                }
            )
            continue
        rate = snapshot_exchange_rate(
            security.currency,
            target_currency,
            on_date,
            ledger_snapshot,
        )
        position.fx_rate = rate
        position.market_value_original = security.market_value_for(
            position.quantity,
            position.price,
        )
        if rate is None:
            missing_rates.append(
                {"account_id": position.account.pk, "currency": security.currency}
            )
            continue
        position.market_value = position.market_value_original * rate
        position.cost = position.cost_original * rate
        total_market_value += position.market_value
        total_cost += position.cost
        if position.pricing_status in {
            PricingStatusChoices.STALE,
            PricingStatusChoices.LEGACY,
            PricingStatusChoices.EXPIRED_UNRESOLVED,
        }:
            stale_prices.append(
                {
                    "account_id": position.account.pk,
                    "security_id": security.pk,
                    "security": f"{security.market}:{security.symbol}",
                    "price_as_of": str(position.price_as_of or ""),
                    "status": position.pricing_status,
                }
            )

    errors = [
        {"account_id": state["account"].pk, "message": message}
        for state in states.values()
        for message in state["errors"]
    ]
    return {
        "positions": positions,
        "cash_lines": cash_lines,
        "total_cash": total_cash,
        "total_market_value": total_market_value,
        "total_cost": total_cost,
        "total_asset": total_cash + total_market_value,
        "total_pnl": total_market_value - total_cost,
        "missing_rates": missing_rates,
        "stale_prices": stale_prices,
        "missing_prices": missing_prices,
        "errors": errors,
        "complete": not missing_rates and not missing_prices and not errors,
    }


def slice_valuation(valuation, account_ids):
    account_ids = set(account_ids)
    positions = [
        item for item in valuation["positions"] if item.account.pk in account_ids
    ]
    cash_lines = [
        item for item in valuation["cash_lines"] if item["account_id"] in account_ids
    ]
    total_cash = sum((item["converted"] for item in cash_lines), ZERO)
    valued_positions = [item for item in positions if item.market_value is not None]
    total_market_value = sum(
        (item.market_value for item in valued_positions), ZERO
    )
    total_cost = sum((item.cost for item in valued_positions), ZERO)
    missing_rates = [
        item for item in valuation["missing_rates"] if item["account_id"] in account_ids
    ]
    missing_prices = [
        item for item in valuation["missing_prices"] if item["account_id"] in account_ids
    ]
    stale_prices = [
        item for item in valuation["stale_prices"] if item["account_id"] in account_ids
    ]
    errors = [
        item for item in valuation["errors"] if item["account_id"] in account_ids
    ]
    return {
        "positions": positions,
        "cash_lines": cash_lines,
        "total_cash": total_cash,
        "total_market_value": total_market_value,
        "total_cost": total_cost,
        "total_asset": total_cash + total_market_value,
        "total_pnl": total_market_value - total_cost,
        "missing_rates": missing_rates,
        "stale_prices": stale_prices,
        "missing_prices": missing_prices,
        "errors": errors,
        "complete": not missing_rates and not missing_prices and not errors,
    }
