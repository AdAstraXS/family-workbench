"""Futu-backed stock detail cache and transparent daily-price observations."""

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
import socket

from django.conf import settings
from django.utils import timezone

from .market_data import futu_code_for_security
from .models import Security, StockMarketResearchSnapshot


def number(value):
    if value is None or value == "":
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def as_text(value):
    value = number(value)
    return str(value) if value is not None else None


def rating_label(value):
    return {
        1: "卖出", 2: "减持", 3: "持有", 4: "买入", 5: "强烈买入",
        "SELL": "卖出", "UNDERPERFORM": "减持", "HOLD": "持有",
        "BUY": "买入", "STRONG_BUY": "强烈买入",
    }.get(value, "未分类")


def mean(values):
    return sum(values, Decimal("0")) / Decimal(len(values)) if values else None


def technical_observations(candles, reference_price=None):
    """Use adjusted closes for trend; identify repeated daily turning-point zones."""
    rows = [row for row in candles if number(row.get("close")) is not None]
    closes = [number(row["close"]) for row in rows]
    volumes = [number(row.get("volume")) for row in rows]
    result = {"as_of": rows[-1]["date"] if rows else None, "count": len(rows)}
    if not rows:
        return result
    for period in (20, 60, 200):
        result[f"ma{period}"] = as_text(mean(closes[-period:])) if len(closes) >= period else None
    if len(closes) >= 15:
        differences = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        gain = mean([max(item, Decimal("0")) for item in differences[:14]])
        loss = mean([max(-item, Decimal("0")) for item in differences[:14]])
        for difference in differences[14:]:
            gain = (gain * 13 + max(difference, Decimal("0"))) / 14
            loss = (loss * 13 + max(-difference, Decimal("0"))) / 14
        if loss == 0:
            result["rsi14"] = "100" if gain else "50"
        else:
            result["rsi14"] = as_text(Decimal("100") - Decimal("100") / (1 + gain / loss))
    if len(volumes) >= 21 and all(value is not None for value in volumes[-21:]):
        baseline = mean(volumes[-21:-1])
        result["volume_ratio20"] = as_text(volumes[-1] / baseline) if baseline else None
    current = number(reference_price) or closes[-1]
    # Five-bar pivots, then group prices within 2%. A zone requires two separate pivots.
    recent = rows[-120:]
    for kind, field, sign in (("support", "low", -1), ("resistance", "high", 1)):
        pivots = []
        for index in range(2, len(recent) - 2):
            value = number(recent[index].get(field))
            neighbours = [number(recent[i].get(field)) for i in range(index - 2, index + 3) if i != index]
            if value is None or any(item is None for item in neighbours):
                continue
            if (value <= min(neighbours) if sign < 0 else value >= max(neighbours)):
                if (value < current if sign < 0 else value > current):
                    pivots.append((value, recent[index]["date"]))
        groups = []
        for value, observed in pivots:
            group = next((group for group in groups if abs(value - group[0][0]) <= current * Decimal("0.02")), None)
            if group is None:
                group = []
                groups.append(group)
            group.append((value, observed))
        eligible = [group for group in groups if len(group) >= 2]
        if eligible:
            selected = min(eligible, key=lambda group: abs(mean([item[0] for item in group]) - current))
            result[kind] = {
                "low": as_text(min(item[0] for item in selected)),
                "high": as_text(max(item[0] for item in selected)),
                "touches": len(selected),
                "last_date": max(item[1] for item in selected),
            }
    return result


def _fetch_section(context, name, callback, errors):
    try:
        response = callback()
        ret, data = response[:2]
        if ret != 0:
            raise ValueError(str(data)[:200])
        return data
    except Exception as exc:
        errors[name] = str(exc)[:240]
        return None


def fetch_stock_research(security):
    if security.asset_type != Security.TYPE_STOCK:
        raise ValueError("个股行情与估值只支持股票。")
    code = futu_code_for_security(security)
    if not code:
        raise ValueError("这只股票尚未配置富途代码。")
    try:
        from futu import OpenQuoteContext
    except ImportError as exc:
        raise ValueError("当前服务未安装富途 API。") from exc
    try:
        socket.create_connection((settings.FUTU_OPEND_HOST, settings.FUTU_OPEND_PORT), timeout=2).close()
    except OSError as exc:
        raise ValueError("暂时无法连接富途 OpenD；已有数据仍可查看。") from exc

    snapshot, _ = StockMarketResearchSnapshot.objects.get_or_create(security=security)
    snapshot.last_attempt_at = timezone.now()
    errors = {}
    updated = False
    context = OpenQuoteContext(host=settings.FUTU_OPEND_HOST, port=settings.FUTU_OPEND_PORT)
    try:
        quote = _fetch_section(context, "quote", lambda: context.get_market_snapshot([code]), errors)
        if quote is not None and not quote.empty:
            item = quote.iloc[0]
            price = number(item.get("last_price"))
            previous_close = number(item.get("prev_close_price"))
            change_rate = (
                (price / previous_close - 1) * 100
                if price is not None and previous_close is not None and previous_close > 0
                else None
            )
            snapshot.quote = {
                "as_of": str(item.get("update_time") or "")[:19],
                "price": as_text(item.get("last_price")),
                "change_rate": as_text(change_rate),
                "previous_close": as_text(item.get("prev_close_price")),
                "open": as_text(item.get("open_price")),
                "high": as_text(item.get("high_price")),
                "low": as_text(item.get("low_price")),
                "volume": as_text(item.get("volume")),
                "market_cap": as_text(item.get("total_market_val")),
                "pe_ttm": as_text(item.get("pe_ttm_ratio")),
                "pb": as_text(item.get("pb_ratio")),
                "ps": as_text(item.get("ps_ratio")),
                "high_52w": as_text(item.get("highest52weeks_price")),
                "low_52w": as_text(item.get("lowest52weeks_price")),
            }
            updated = True
        elif quote is not None:
            errors["quote"] = "富途没有返回这只股票的行情。"

        start = (date.today() - timedelta(days=450)).isoformat()
        bars = _fetch_section(
            context, "candles",
            lambda: context.request_history_kline(code, start=start, end=date.today().isoformat(), max_count=500),
            errors,
        )
        if bars is not None and not bars.empty:
            snapshot.candles = [
                {
                    "date": str(item.get("time_key"))[:10],
                    "open": as_text(item.get("open")),
                    "high": as_text(item.get("high")),
                    "low": as_text(item.get("low")),
                    "close": as_text(item.get("close")),
                    "volume": as_text(item.get("volume")),
                }
                for item in bars.to_dict("records")
            ]
            updated = True
        elif bars is not None:
            errors["candles"] = "没有可用的历史日线。"

        valuation = dict(snapshot.valuation or {})
        for metric, metric_type in (("pe", 1), ("pb", 2), ("ps", 3)):
            metric_data = {}
            for period, interval in (("1y", 3), ("3y", 4), ("5y", 6)):
                data = _fetch_section(
                    context, f"{metric}_{period}",
                    lambda m=metric_type, i=interval: context.get_valuation_detail(code, valuation_type=m, interval_type=i),
                    errors,
                )
                if data is None:
                    continue
                trend = data.get("trend") or {}
                metric_data[period] = {
                    "as_of": data.get("last_update_time_str"),
                    "current": as_text(trend.get("current_value")),
                    "average": as_text(trend.get("average_value")),
                    "percentile": as_text(trend.get("valuation_percentile")),
                    "forward": as_text(trend.get("forward_value")),
                    "series": [
                        {"date": row.get("time_str"), "value": as_text(row.get("value"))}
                        for row in (trend.get("historical_items") or [])[::max(1, len(trend.get("historical_items") or []) // 260)]
                        if row.get("time_str") and as_text(row.get("value")) is not None
                    ],
                }
                updated = True
            if metric_data:
                valuation[metric] = {**valuation.get(metric, {}), **metric_data}
        snapshot.valuation = valuation

        consensus = _fetch_section(
            context, "analysts", lambda: context.get_research_analyst_consensus(code), errors
        )
        ratings = _fetch_section(
            context, "ratings", lambda: context.get_research_rating_summary(code, num=10), errors
        )
        if consensus:
            institutions = []
            for row in (ratings or {}).get("inst_rating_summary_list") or []:
                info = row.get("institution_info") or {}
                latest = (row.get("rating_item_list") or [{}])[0]
                institutions.append({
                    "name": info.get("institution_source_name") or info.get("institution_name"),
                    "rating": latest.get("rating"),
                    "rating_label": rating_label(latest.get("rating")),
                    "target": as_text(latest.get("target_price")),
                    "date": latest.get("recommendation_date_str"),
                    "url": latest.get("rating_url") if str(latest.get("rating_url") or "").startswith("https://") else "",
                })
            snapshot.analysts = {
                "as_of": consensus.get("update_time_str"),
                "total": consensus.get("total"),
                "rating": consensus.get("rating"),
                "buy": as_text(consensus.get("buy")),
                "hold": as_text(consensus.get("hold")),
                "sell": as_text(consensus.get("sell")),
                "target_high": as_text(consensus.get("highest")),
                "target_avg": as_text(consensus.get("average")),
                "target_low": as_text(consensus.get("lowest")),
                "institutions": institutions,
            }
            updated = True

        morningstar = _fetch_section(
            context, "morningstar", lambda: context.get_research_morningstar_report(code), errors
        )
        if morningstar:
            snapshot.morningstar = {
                "as_of": morningstar.get("star_update_time_str"),
                "stars": morningstar.get("star_rating"),
                "fair_value": as_text(morningstar.get("fair_value")),
                "moat": morningstar.get("economic_moat_label"),
                "uncertainty": morningstar.get("uncertainty_label"),
                "report_date": morningstar.get("analyst_report_update_time_str"),
            }
            updated = True
    finally:
        context.close()
    snapshot.errors = errors
    if updated:
        snapshot.fetched_at = timezone.now()
    snapshot.save()
    snapshot._refreshed_any = updated
    return snapshot
