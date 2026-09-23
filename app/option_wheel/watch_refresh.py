"""Explicit Futu refreshes for the small option watchlist."""

from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from django.conf import settings
from django.utils import timezone

from portfolio.futu_service import get_futu_market_snapshots
from portfolio.futu_option_probe import _fetch_dividend_calendar, records_from, sdk_call

from .models import WheelWatchItem


NY = ZoneInfo("America/New_York")


def refresh_watch_prices(family, *, update_names=False):
    items = list(WheelWatchItem.objects.filter(family=family))
    if not items:
        return 0
    records = get_futu_market_snapshots(["US." + item.symbol for item in items])
    updates = []
    for item in items:
        row = records.get("US." + item.symbol)
        if not row:
            continue
        try:
            price = Decimal(str(row["last_price"]))
            as_of = datetime.fromisoformat(str(row["quote_time"]))
        except (InvalidOperation, TypeError, ValueError):
            continue
        if not price.is_finite() or price <= 0:
            continue
        if timezone.is_naive(as_of):
            as_of = as_of.replace(tzinfo=NY)
        item.price = price
        item.price_as_of = as_of
        if update_names and row.get("name"):
            item.name = str(row["name"])[:100]
        item.updated_at = timezone.now()
        updates.append(item)
    fields = ["price", "price_as_of", "updated_at"]
    if update_names:
        fields.append("name")
    WheelWatchItem.objects.bulk_update(updates, fields)
    return len(updates)


def refresh_watch_events(family, *, days=35):
    """Calendar is queried for the selected DTE horizon; no missing date is invented."""
    items = list(WheelWatchItem.objects.filter(family=family))
    if not items:
        return 0
    from futu import Market, OpenQuoteContext, RET_OK

    today = timezone.now().astimezone(NY).date()
    context = OpenQuoteContext(host=settings.FUTU_OPEND_HOST, port=settings.FUTU_OPEND_PORT)
    try:
        earnings_rows = []
        for offset in range(0, days + 1, 7):
            earnings_response = sdk_call(
                context, "get_earnings_calendar", RET_OK, Market.US,
                begin_date=str(today + timedelta(days=offset)),
                end_date=str(today + timedelta(days=min(offset + 6, days))),
            )
            if earnings_response["status"] != "ok":
                raise ValueError("earnings calendar unavailable")
            earnings_rows.extend(records_from(earnings_response["data"]))
        dividend_rows = []
        for offset in range(days + 1):
            result, rows = _fetch_dividend_calendar(context, RET_OK, Market.US, today + timedelta(days=offset))
            if result["status"] != "ok":
                raise ValueError("dividend calendar unavailable")
            dividend_rows.extend(rows)
    finally:
        context.close()
    def next_date(rows, symbol, key):
        dates = []
        for row in rows:
            if str(row.get("security") or row.get("code") or "").upper() != "US." + symbol:
                continue
            try:
                parsed = datetime.fromisoformat(str(row.get(key) or "")[:10]).date()
            except ValueError:
                continue
            if parsed >= today:
                dates.append(parsed)
        return min(dates) if dates else None
    now = timezone.now()
    for item in items:
        item.next_earnings = next_date(earnings_rows, item.symbol, "earnings_date")
        item.next_dividend = next_date(dividend_rows, item.symbol, "ex_date")
        item.events_checked_at = now
        item.events_covered_until = today + timedelta(days=days)
        item.updated_at = now
    WheelWatchItem.objects.bulk_update(items, ["next_earnings", "next_dividend", "events_checked_at", "events_covered_until", "updated_at"])
    return len(items)
