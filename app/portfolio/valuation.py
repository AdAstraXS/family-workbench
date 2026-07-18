from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.db.models import Sum
from django.utils import timezone

from family_core.models import ExchangeRate

from .market_data import quote_config_for_security
from .models import (
    InvestmentCashMovement,
    InvestmentPosition,
    PriceSourceChoices,
    PricingStatusChoices,
    SecurityPriceRecord,
)


ZERO = Decimal("0")


@dataclass
class PriceResolution:
    price: Decimal | None
    source: str = ""
    status: str = PricingStatusChoices.MISSING
    price_as_of: datetime | None = None
    record: SecurityPriceRecord | None = None


def _observation_status(config, source, price_as_of, reference_time):
    if not price_as_of:
        return PricingStatusChoices.MISSING
    if price_as_of < reference_time - timedelta(hours=config.max_age_hours):
        return PricingStatusChoices.STALE
    if source == PriceSourceChoices.MANUAL:
        return PricingStatusChoices.MANUAL
    if source == PriceSourceChoices.LEGACY:
        return PricingStatusChoices.STALE
    return PricingStatusChoices.FRESH


def resolve_position_prices(positions, on_date=None):
    positions = list(positions)
    on_date = on_date or date.today()
    cutoff = timezone.make_aware(datetime.combine(on_date, time.max))
    security_ids = {position.security_id for position in positions}
    records = {}
    for item in (
        SecurityPriceRecord.objects.filter(
            security_id__in=security_ids,
            price_as_of__lte=cutoff,
        )
        .select_related("security")
        .order_by("security_id", "-price_as_of", "-pk")
    ):
        records.setdefault(item.security_id, item)

    resolutions = {}
    today = timezone.localdate()
    reference_time = timezone.now() if on_date == today else cutoff
    for position in positions:
        security = position.security
        config = quote_config_for_security(security)
        record = records.get(security.pk)
        snapshot = getattr(security, "market_snapshot", None)
        if record:
            resolution = PriceResolution(
                price=record.price,
                source=record.source,
                status=_observation_status(
                    config,
                    record.source,
                    record.price_as_of,
                    reference_time,
                ),
                price_as_of=record.price_as_of,
                record=record,
            )
            if (
                on_date == today
                and snapshot
                and snapshot.last_error
                and snapshot.last_attempt_at
                and snapshot.last_attempt_at >= snapshot.fetched_at
            ):
                resolution.status = PricingStatusChoices.ERROR
        elif snapshot and snapshot.last_price is not None and (
            not snapshot.price_as_of or snapshot.price_as_of <= cutoff
        ):
            price_as_of = snapshot.price_as_of or snapshot.fetched_at
            resolution = PriceResolution(
                price=snapshot.last_price,
                source=snapshot.price_source,
                status=_observation_status(
                    config,
                    snapshot.price_source,
                    price_as_of,
                    reference_time,
                ),
                price_as_of=price_as_of,
            )
        elif position.current_price and (
            not position.current_price_as_of
            or position.current_price_as_of <= cutoff
        ):
            resolution = PriceResolution(
                price=position.current_price,
                source=position.current_price_source or PriceSourceChoices.LEGACY,
                status=PricingStatusChoices.STALE,
                price_as_of=position.current_price_as_of,
            )
        else:
            resolution = PriceResolution(price=None)

        option = getattr(security, "option_contract", None)
        if option and option.expiration_date < on_date and position.quantity:
            resolution.status = PricingStatusChoices.EXPIRED_UNRESOLVED
        resolutions[position.pk] = resolution
    return resolutions


def refresh_position_valuations(*, security_ids=None, on_date=None):
    positions = list(
        InvestmentPosition.objects.exclude(quantity=0)
        .filter(**({"security_id__in": security_ids} if security_ids is not None else {}))
        .select_related(
            "security__market_snapshot",
            "security__option_contract",
            "security__bond_detail",
        )
        .prefetch_related("security__quote_configs")
    )
    resolutions = resolve_position_prices(positions, on_date)
    changed = []
    for position in positions:
        resolution = resolutions[position.pk]
        position.pricing_status = resolution.status
        position.current_price_source = resolution.source or PriceSourceChoices.LEGACY
        position.current_price_as_of = resolution.price_as_of
        if resolution.price is not None:
            position.current_price = resolution.price
            position.market_value = position.security.market_value_for(
                position.quantity,
                resolution.price,
            )
            cost = position.quantity * position.avg_cost * position.security.contract_multiplier
            position.unrealized_pnl = position.market_value - cost
            position.pnl_ratio = position.unrealized_pnl / cost if cost else ZERO
        changed.append(position)
    if changed:
        InvestmentPosition.objects.bulk_update(
            changed,
            [
                "current_price",
                "current_price_as_of",
                "current_price_source",
                "pricing_status",
                "market_value",
                "unrealized_pnl",
                "pnl_ratio",
            ],
        )
    return positions


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
    if amount is None:
        return None
    rate = exchange_rate(source, target, on_date)
    return None if rate is None else amount * rate


def value_portfolio(accounts, target_currency, on_date, *, refresh_positions=False):
    accounts = list(accounts)
    positions = list(
        InvestmentPosition.objects.filter(account__in=accounts).exclude(quantity=0)
        .select_related(
            "account__bank_account",
            "security",
            "security__market_snapshot",
            "security__option_contract",
            "security__bond_detail",
        )
        .prefetch_related("security__quote_configs")
        .order_by("account_id", "security__symbol")
    )
    resolutions = resolve_position_prices(positions, on_date)
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
    stale_prices = False
    missing_prices = False
    for position in positions:
        resolution = resolutions[position.pk]
        price = resolution.price
        position.valuation_price_source = resolution.source
        position.valuation_price_as_of = resolution.price_as_of
        position.valuation_pricing_status = resolution.status
        if resolution.status in {
            PricingStatusChoices.STALE,
            PricingStatusChoices.ERROR,
            PricingStatusChoices.EXPIRED_UNRESOLVED,
        }:
            stale_prices = True
        if price is None:
            position.valuation_price = None
            position.valuation_fx_rate = None
            position.valuation_market_value_original = None
            position.valuation_market_value = None
            position.valuation_cost_original = (
                position.quantity
                * position.avg_cost
                * position.security.contract_multiplier
            )
            position.valuation_cost = None
            missing_prices = True
            if refresh_positions:
                position.pricing_status = resolution.status
                position.current_price_source = resolution.source or PriceSourceChoices.LEGACY
                position.current_price_as_of = resolution.price_as_of
                changed.append(position)
            continue
        rate = exchange_rate(position.security.currency, target_currency, on_date)
        position.valuation_price = price
        position.valuation_fx_rate = rate
        multiplier = position.security.contract_multiplier
        position.valuation_market_value_original = position.security.market_value_for(
            position.quantity, price
        )
        position.valuation_cost_original = position.quantity * position.avg_cost * multiplier
        if rate is None:
            position.valuation_market_value = None
            position.valuation_cost = None
            missing_rates = True
            continue
        position.valuation_market_value = position.valuation_market_value_original * rate
        position.valuation_cost = position.valuation_cost_original * rate
        total_market_value += position.valuation_market_value
        total_cost += position.valuation_cost
        if refresh_positions:
            position.current_price = price
            position.current_price_as_of = resolution.price_as_of
            position.current_price_source = resolution.source or PriceSourceChoices.LEGACY
            position.pricing_status = resolution.status
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
            [
                "current_price",
                "current_price_as_of",
                "current_price_source",
                "pricing_status",
                "market_value",
                "unrealized_pnl",
                "pnl_ratio",
            ],
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
        "stale_prices": stale_prices,
        "missing_prices": missing_prices,
    }
