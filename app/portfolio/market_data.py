from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from django.db import transaction
from django.utils import timezone

from .futu_service import FutuQueryError, get_futu_market_snapshots
from .models import (
    InvestmentPosition,
    MarketDataRefreshRun,
    MarketDataRunStatusChoices,
    PriceSourceChoices,
    PricingStatusChoices,
    Security,
    SecurityExchange,
    SecurityMarketSnapshot,
    SecurityPriceRecord,
    SecurityQuoteConfig,
    WatchlistItem,
)


def futu_code_for_security(security):
    explicit = (security.extra_data or {}).get("futu_code")
    if explicit:
        return str(explicit).strip().upper()
    symbol = (security.symbol or "").strip().upper()
    if symbol.startswith(("HK.", "US.", "SH.", "SZ.")):
        return symbol
    for suffix in (".HK", ".US", ".SH", ".SZ"):
        if symbol.endswith(suffix):
            symbol = symbol[: -len(suffix)]
            break
    if security.exchange:
        exchange = (
            SecurityExchange.objects.select_related("market")
            .filter(
                market__code=security.market,
                code=security.exchange,
                market__is_active=True,
                market__supports_futu=True,
                is_active=True,
            )
            .first()
        )
        if exchange and exchange.provider_prefix and symbol:
            return f"{exchange.provider_prefix}.{symbol}"
    if security.market == "HK" and symbol.isdigit():
        return f"HK.{symbol.zfill(5)}"
    if security.market == "US" and symbol:
        return f"US.{symbol}"
    if security.market in {"CN", "CN_B"} and symbol.isdigit():
        if security.exchange == "BJ":
            return ""
        exchange = security.exchange if security.exchange in {"SH", "SZ"} else (
            "SH" if symbol.startswith(("5", "6", "9")) else "SZ"
        )
        return f"{exchange}.{symbol}"
    return ""


def default_quote_config(security):
    provider_symbol = futu_code_for_security(security)
    automatic = security.asset_type in {Security.TYPE_STOCK, Security.TYPE_ETF} and bool(
        provider_symbol
    )
    if (security.extra_data or {}).get("futu_code"):
        automatic = True
    provider = PriceSourceChoices.FUTU if automatic else PriceSourceChoices.MANUAL
    return SecurityQuoteConfig(
        security=security,
        provider=provider,
        provider_symbol=provider_symbol if automatic else "",
        price_type="last" if automatic else "manual",
        max_age_hours=96 if automatic else 720,
    )


def quote_config_for_security(security):
    cached = getattr(security, "_prefetched_objects_cache", {}).get("quote_configs")
    if cached is not None:
        ordered = sorted(cached, key=lambda item: (item.priority, item.pk))
        enabled = [item for item in ordered if item.enabled]
        return (enabled or ordered or [default_quote_config(security)])[0]
    config = security.quote_configs.filter(enabled=True).order_by("priority", "pk").first()
    if not config:
        config = security.quote_configs.order_by("priority", "pk").first()
    return config or default_quote_config(security)


def ensure_quote_config(security):
    config = quote_config_for_security(security)
    if config.pk:
        return config
    config.save()
    cached = getattr(security, "_prefetched_objects_cache", {}).get("quote_configs")
    if cached is not None:
        if hasattr(cached, "_result_cache"):
            cached._result_cache.append(config)
        else:
            cached.append(config)
    return config


def parse_futu_quote_time(value, market, fallback=None):
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.strptime(str(value or "")[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return fallback or timezone.now()
    if timezone.is_naive(parsed):
        zone = ZoneInfo("America/New_York" if market == "US" else "Asia/Shanghai")
        parsed = parsed.replace(tzinfo=zone)
    return parsed.astimezone(ZoneInfo("UTC"))


def quote_status(config, snapshot, now=None):
    now = now or timezone.now()
    if not snapshot or snapshot.last_price is None:
        return PricingStatusChoices.MISSING
    if snapshot.last_error and snapshot.last_attempt_at and snapshot.last_attempt_at >= snapshot.fetched_at:
        return PricingStatusChoices.ERROR
    price_as_of = snapshot.price_as_of or snapshot.fetched_at
    if not price_as_of or price_as_of < now - timedelta(hours=config.max_age_hours):
        return PricingStatusChoices.STALE
    if snapshot.price_source == PriceSourceChoices.MANUAL:
        return PricingStatusChoices.MANUAL
    if snapshot.price_source == PriceSourceChoices.LEGACY:
        return PricingStatusChoices.STALE
    return PricingStatusChoices.FRESH


def _decimal(value):
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


@transaction.atomic
def record_security_price(
    security,
    price,
    *,
    source,
    price_as_of,
    price_type="last",
    refresh_run=None,
    is_delayed=False,
    quote_data=None,
):
    price = _decimal(price)
    if price is None or price < 0:
        raise ValueError("行情价格无效。")
    if price == 0 and security.asset_type != Security.TYPE_OPTION:
        raise ValueError("非期权标的的行情价格必须大于 0。")
    if timezone.is_naive(price_as_of):
        price_as_of = timezone.make_aware(price_as_of)
    quote_data = quote_data or {}
    record, _ = SecurityPriceRecord.objects.update_or_create(
        security=security,
        source=source,
        price_type=price_type,
        price_as_of=price_as_of,
        defaults={
            "refresh_run": refresh_run,
            "price": price,
            "currency": security.currency,
            "is_delayed": is_delayed,
            "raw_data": quote_data.get("raw_data") or {},
        },
    )
    current = SecurityMarketSnapshot.objects.filter(security=security).first()
    if current and current.price_as_of and current.price_as_of > price_as_of:
        return record
    config = ensure_quote_config(security)
    status = (
        PricingStatusChoices.MANUAL
        if source == PriceSourceChoices.MANUAL
        else PricingStatusChoices.FRESH
    )
    defaults = {
        "quote_time": quote_data.get("quote_time") or price_as_of.isoformat(),
        "last_price": price,
        "change_rate": _decimal(quote_data.get("change_rate")),
        "total_market_value": _decimal(quote_data.get("total_market_value")),
        "pe_ratio": _decimal(quote_data.get("pe_ratio")),
        "pe_ttm_ratio": _decimal(quote_data.get("pe_ttm_ratio")),
        "pb_ratio": _decimal(quote_data.get("pb_ratio")),
        "ps_ratio": _decimal(quote_data.get("ps_ratio")),
        "dividend_yield_ttm": _decimal(quote_data.get("dividend_yield_ttm")),
        "turnover_rate": _decimal(quote_data.get("turnover_rate")),
        "high_52_week": _decimal(quote_data.get("high_52_week")),
        "low_52_week": _decimal(quote_data.get("low_52_week")),
        "issued_shares": quote_data.get("issued_shares"),
        "outstanding_shares": quote_data.get("outstanding_shares"),
        "raw_data": quote_data.get("raw_data") or {},
        "price_source": source,
        "price_as_of": price_as_of,
        "pricing_status": status,
        "is_delayed": is_delayed,
        "last_attempt_at": timezone.now(),
        "last_error": "",
        "refresh_run": refresh_run,
    }
    snapshot, _ = SecurityMarketSnapshot.objects.update_or_create(
        security=security,
        defaults=defaults,
    )
    actual_status = quote_status(config, snapshot)
    if snapshot.pricing_status != actual_status:
        SecurityMarketSnapshot.objects.filter(pk=snapshot.pk).update(
            pricing_status=actual_status
        )
    return record


def mark_quote_error(security, message, refresh_run=None):
    now = timezone.now()
    updated = SecurityMarketSnapshot.objects.filter(security=security).update(
        pricing_status=PricingStatusChoices.ERROR,
        last_attempt_at=now,
        last_error=str(message)[:2000],
        refresh_run=refresh_run,
    )
    if not updated:
        SecurityMarketSnapshot.objects.create(
            security=security,
            pricing_status=PricingStatusChoices.MISSING,
            last_attempt_at=now,
            last_error=str(message)[:2000],
            refresh_run=refresh_run,
        )


def market_data_targets(*, include_watchlist=False, security_ids=None):
    if security_ids is not None:
        return list(
            Security.objects.filter(pk__in=security_ids, is_active=True)
            .prefetch_related("quote_configs")
        )
    ids = set(
        InvestmentPosition.objects.exclude(quantity=0).values_list("security_id", flat=True)
    )
    if include_watchlist:
        ids.update(
            WatchlistItem.objects.filter(is_active=True).values_list("security_id", flat=True)
        )
    return list(
        Security.objects.filter(pk__in=ids, is_active=True)
        .prefetch_related("quote_configs")
        .order_by("market", "symbol")
    )


def refresh_market_data(*, include_watchlist=False, security_ids=None):
    scope = "selected" if security_ids is not None else (
        "holdings_watchlist" if include_watchlist else "holdings"
    )
    run = MarketDataRefreshRun.objects.create(scope=scope)
    securities = market_data_targets(
        include_watchlist=include_watchlist,
        security_ids=security_ids,
    )
    configs = {security.pk: ensure_quote_config(security) for security in securities}
    automatic = [
        config
        for config in configs.values()
        if config.enabled and config.provider == PriceSourceChoices.FUTU
    ]
    errors = []
    success_count = 0
    quotes = {}
    if automatic:
        try:
            quotes = get_futu_market_snapshots(
                [config.provider_symbol for config in automatic]
            )
        except FutuQueryError as exc:
            errors.append(str(exc))
            for config in automatic:
                mark_quote_error(config.security, exc, run)
        else:
            for config in automatic:
                security = config.security
                quote = quotes.get(config.provider_symbol)
                if not quote or quote.get("last_price") in (None, ""):
                    message = f"{config.provider_symbol} 未返回有效最新价"
                    errors.append(message)
                    mark_quote_error(security, message, run)
                    continue
                try:
                    record_security_price(
                        security,
                        quote["last_price"],
                        source=PriceSourceChoices.FUTU,
                        price_as_of=parse_futu_quote_time(
                            quote.get("quote_time"), security.market
                        ),
                        price_type=config.price_type,
                        refresh_run=run,
                        quote_data=quote,
                    )
                except ValueError as exc:
                    errors.append(f"{config.provider_symbol}: {exc}")
                    mark_quote_error(security, exc, run)
                else:
                    success_count += 1

    from .valuation import refresh_position_valuations

    refresh_position_valuations(security_ids=[security.pk for security in securities])
    stale_count = 0
    missing_count = 0
    for security in securities:
        config = configs[security.pk]
        snapshot = SecurityMarketSnapshot.objects.filter(security=security).first()
        status = quote_status(config, snapshot)
        if status in {PricingStatusChoices.STALE, PricingStatusChoices.ERROR}:
            stale_count += 1
        elif status == PricingStatusChoices.MISSING:
            missing_count += 1
    error_count = len(errors)
    if error_count and success_count:
        status = MarketDataRunStatusChoices.PARTIAL
    elif error_count:
        status = MarketDataRunStatusChoices.FAILED
    else:
        status = MarketDataRunStatusChoices.SUCCESS
    run.finished_at = timezone.now()
    run.status = status
    run.target_count = len(automatic)
    run.success_count = success_count
    run.stale_count = stale_count
    run.missing_count = missing_count
    run.error_count = error_count
    run.details = {
        "security_count": len(securities),
        "manual_count": len(securities) - len(automatic),
        "errors": errors[:100],
    }
    run.save()
    return run
