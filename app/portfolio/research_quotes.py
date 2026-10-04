"""Read saved, dated prices for research without changing portfolio valuation data."""
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from .models import SecurityMarketSnapshot, StockMarketResearchSnapshot


def positive_number(value):
    try:
        number = Decimal(str(value))
        return number if number.is_finite() and number > 0 else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def market_zone(security):
    return ZoneInfo("America/New_York" if security.market == "US" else "Asia/Shanghai")


def normalize_quote(security, raw):
    """Reject missing dates and future observations; never use acquisition time as price time."""
    price = positive_number(raw.get("price"))
    stamp = str(raw.get("price_as_of") or raw.get("as_of") or "")
    try:
        observed = parse_datetime(stamp) if len(stamp) > 10 else parse_date(stamp)
    except (ValueError, TypeError):
        return None
    if not price or not observed:
        return None
    now = timezone.now()
    if isinstance(observed, datetime):
        if timezone.is_naive(observed):
            observed = observed.replace(tzinfo=market_zone(security))
        if observed > now + timedelta(minutes=5):
            return None
        observed_date = observed.astimezone(market_zone(security)).date()
        label = timezone.localtime(observed).strftime("%Y-%m-%d %H:%M %Z")
        rank = observed
        precision = "time"
    else:
        observed_date = observed
        label = observed.isoformat() + "（交易日）"
        rank = datetime.combine(observed, datetime.min.time(), tzinfo=market_zone(security))
        precision = "day"
    if observed_date > now.astimezone(market_zone(security)).date():
        return None
    price_type = raw.get("price_type", "last")
    if price_type == "close" and raw.get("adjustment") != "none":
        return None
    return {
        **raw, "price": price, "currency": security.currency,
        "price_as_of": observed, "as_of": label, "as_of_label": label,
        "date_precision": precision, "price_type": price_type,
        "price_label": "最近已保存收盘价（未复权）" if price_type == "close" else "已保存报价",
        "price_source": raw.get("price_source", "富途行情快照"),
        "pe_ttm": positive_number(raw.get("pe_ttm")),
        "is_delayed": raw.get("is_delayed"),
        "is_stale": rank < now - timedelta(hours=72),
        "_rank": rank,
    }


def saved_research_quote(security, *, stock_snapshot=None):
    """Pick the newest usable price across the existing two caches; GET stays read-only."""
    stock = stock_snapshot or StockMarketResearchSnapshot.objects.filter(security=security).first()
    candidates = []
    if stock and stock.quote:
        quote = normalize_quote(security, stock.quote)
        if quote:
            candidates.append(quote)
    market = SecurityMarketSnapshot.objects.filter(security=security).first()
    if market:
        quote = normalize_quote(security, {
            "price": market.last_price, "price_as_of": market.price_as_of,
            "price_source": market.get_price_source_display(),
            "pricing_status": market.get_pricing_status_display(),
            "is_delayed": market.is_delayed, "pe_ttm": market.pe_ttm_ratio,
            "change_rate": market.change_rate, "market_cap": market.total_market_value,
            "pb": market.pb_ratio, "ps": market.ps_ratio,
            "high_52w": market.high_52_week, "low_52w": market.low_52_week,
        })
        if quote:
            candidates.append(quote)
    return max(candidates, key=lambda item: item["_rank"]) if candidates else {}


def freeze_research_quote(quote):
    if not quote:
        return {}
    keys = ("price", "currency", "price_as_of", "price_source", "pricing_status",
            "is_delayed", "is_stale", "pe_ttm", "price_type", "adjustment", "date_precision",
            "as_of_label", "price_label")
    result = {key: quote[key] for key in keys if key in quote}
    return {key: value.isoformat() if isinstance(value, (date, datetime)) else
            str(value) if isinstance(value, Decimal) else value for key, value in result.items()}
