import re
import socket

from django.conf import settings


class FutuQueryError(Exception):
    pass


def _clean(value):
    if value is None or value != value:
        return None
    if hasattr(value, "item"):
        value = value.item()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _code_candidate(keyword, market):
    value = keyword.strip().upper().replace(" ", "")
    if "." in value:
        left, right = value.split(".", 1)
        if left in {"HK", "US", "SH", "SZ"}:
            return value
        if right in {"HK", "US", "SH", "SZ"}:
            return f"{right}.{left}"
    if market == "HK" and value.isdigit():
        return f"HK.{value.zfill(5)}"
    if market == "US" and re.fullmatch(r"[A-Z][A-Z0-9.-]{0,9}", value):
        return f"US.{value}"
    if market == "CN" and value.isdigit() and len(value) == 6:
        return f"{'SH' if value.startswith(('5', '6', '9')) else 'SZ'}.{value}"
    return None


def _futu_url(code):
    exchange, symbol = code.split(".", 1)
    prefix = "/hk" if exchange == "HK" else ""
    return f"https://www.futunn.com{prefix}/stock/{symbol}-{exchange}"


def search_futu_securities(keyword, market):
    try:
        from futu import (
            Market,
            OpenQuoteContext,
            RET_OK,
            SecurityType,
        )
    except ImportError as exc:
        raise FutuQueryError("Futu SDK 尚未安装。") from exc

    market_map = {"HK": Market.HK, "US": Market.US, "CN": Market.SH}
    if market not in market_map:
        raise FutuQueryError("暂时只支持港股、美股和 A 股。")

    try:
        socket.create_connection(
            (settings.FUTU_OPEND_HOST, settings.FUTU_OPEND_PORT),
            timeout=2,
        ).close()
    except OSError as exc:
        raise FutuQueryError(
            f"无法连接 Futu OpenD（{settings.FUTU_OPEND_HOST}:{settings.FUTU_OPEND_PORT}），"
            "请先启动 OpenD 并开放 API 监听。"
        ) from exc

    context = OpenQuoteContext(
        host=settings.FUTU_OPEND_HOST,
        port=settings.FUTU_OPEND_PORT,
    )
    try:
        candidate = _code_candidate(keyword, market)
        basic_records = []
        if candidate:
            ret, data = context.get_stock_basicinfo(
                market=market_map[market],
                stock_type=SecurityType.STOCK,
                code_list=[candidate],
            )
            if ret == RET_OK:
                basic_records = data.to_dict("records")

        if not basic_records:
            ret, data = context.get_stock_basicinfo(
                market=market_map[market],
                stock_type=SecurityType.STOCK,
            )
            if ret != RET_OK:
                raise FutuQueryError(str(data))
            keyword_lower = keyword.strip().lower()
            basic_records = [
                item
                for item in data.to_dict("records")
                if keyword_lower in str(item.get("code", "")).lower()
                or keyword_lower in str(item.get("name", "")).lower()
            ][:20]

        codes = [str(item["code"]) for item in basic_records]
        snapshots = {}
        if codes:
            ret, data = context.get_market_snapshot(codes)
            if ret == RET_OK:
                snapshots = {
                    str(item["code"]): item
                    for item in data.to_dict("records")
                }
    except FutuQueryError:
        raise
    except Exception as exc:
        raise FutuQueryError(
            f"无法连接 Futu OpenD（{settings.FUTU_OPEND_HOST}:{settings.FUTU_OPEND_PORT}）：{exc}"
        ) from exc
    finally:
        context.close()

    results = []
    for basic in basic_records:
        code = str(basic["code"])
        exchange, symbol = code.split(".", 1)
        snapshot = snapshots.get(code, {})
        stock_type = str(basic.get("stock_type") or "stock").lower()
        last_price = _clean(snapshot.get("last_price"))
        prev_close_price = _clean(snapshot.get("prev_close_price"))
        change_rate = None
        if last_price is not None and prev_close_price not in (None, 0):
            change_rate = (last_price - prev_close_price) / prev_close_price * 100
        results.append(
            {
                "code": code,
                "symbol": symbol,
                "market": "CN" if exchange in {"SH", "SZ"} else exchange,
                "exchange": exchange,
                "name": str(basic.get("name") or symbol),
                "asset_type": {
                    "stock": "stock",
                    "etf": "etf",
                    "bond": "bond",
                    "index": "index",
                }.get(stock_type, "stock"),
                "currency": {"HK": "HKD", "US": "USD", "SH": "CNY", "SZ": "CNY"}[exchange],
                "lot_size": _clean(basic.get("lot_size")) or 0,
                "listing_date": _clean(basic.get("listing_date")) or "",
                "is_delisted": bool(_clean(basic.get("delisting")) or False),
                "last_price": last_price,
                "change_rate": change_rate,
                "quote_time": _clean(snapshot.get("update_time")) or "",
                "total_market_value": _clean(snapshot.get("total_market_val")),
                "pe_ratio": _clean(snapshot.get("pe_ratio")),
                "pe_ttm_ratio": _clean(snapshot.get("pe_ttm_ratio")),
                "pb_ratio": _clean(snapshot.get("pb_ratio")),
                "ps_ratio": _clean(snapshot.get("ps_ratio")),
                "dividend_yield_ttm": _clean(snapshot.get("dividend_ratio_ttm")),
                "turnover_rate": _clean(snapshot.get("turnover_rate")),
                "high_52_week": _clean(snapshot.get("highest52weeks_price")),
                "low_52_week": _clean(snapshot.get("lowest52weeks_price")),
                "issued_shares": _clean(snapshot.get("issued_shares")),
                "outstanding_shares": _clean(snapshot.get("outstanding_shares")),
                "futu_url": _futu_url(code),
                "raw_data": {
                    "stock_id": _clean(basic.get("stock_id")),
                    "exchange_type": _clean(basic.get("exchange_type")),
                    "suspension": _clean(snapshot.get("suspension")),
                },
            }
        )
    return results
