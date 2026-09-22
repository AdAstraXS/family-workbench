"""Human-readable, account-independent option comparisons from Futu evidence."""

from datetime import date
from decimal import Decimal, InvalidOperation

from portfolio.models import InvestmentPosition

from .views import PARTICIPATING_ACCOUNTS


def number(value):
    if value in (None, "", "N/A") or isinstance(value, bool):
        return None
    try:
        value = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return value if value.is_finite() else None


def field(data, key):
    item = data.get(key, {}) if isinstance(data, dict) else {}
    return number(item.get("value")) if isinstance(item, dict) else None


def text(value, places=2):
    if value is None:
        return None
    return str(value.quantize(Decimal(1).scaleb(-places)))


def covered_stock(family, symbols):
    """Current recorded shares, for context only; no account gates."""
    holdings = {}
    positions = InvestmentPosition.objects.filter(
        account__bank_account__family=family,
        account__bank_account__account_name__in=PARTICIPATING_ACCOUNTS,
        security__symbol__in=symbols,
        security__market__iexact="US",
        security__asset_type="stock",
        quantity__gt=0,
    ).select_related("security", "account__bank_account")
    for position in positions:
        if position.quantity >= 100:
            holdings.setdefault(position.security.symbol.upper(), []).append({
                "account": position.account.account_name,
                "shares": position.quantity,
                "cost": position.avg_cost if position.avg_cost > 0 else None,
            })
    return holdings


def compare_probe_rows(rows, selection, holdings, watch_events):
    premium_min = number(selection["premium_min"])
    premium_max = number(selection["premium_max"])
    expiry = date.fromisoformat(selection["target_expiration"])
    results = []
    for symbol_row in rows:
        symbol = str(symbol_row.get("symbol", "")).upper().removeprefix("US.")
        if symbol not in selection["symbols"]:
            continue
        watch = watch_events.get(symbol)
        overview = symbol_row.get("underlying_iv", {})
        stock_percentile = number(overview.get("iv_percentile"))
        event_risks = []
        if watch is None or watch.events_checked_at is None or watch.events_covered_until is None or watch.events_covered_until < expiry:
            event_risks.append("财报及除息日期未核实")
        else:
            if watch.next_earnings and date.fromisoformat(selection["analysis_date"]) <= watch.next_earnings <= expiry:
                event_risks.append("到期前跨财报" + ("" if selection["allow_earnings"] else "（不符合本次边界）"))
            if watch.next_dividend and date.fromisoformat(selection["analysis_date"]) <= watch.next_dividend <= expiry:
                event_risks.append("到期前跨除息日" + ("" if selection["allow_dividend"] else "（不符合本次边界）"))
        for item in symbol_row.get("representative_contracts", []):
            kind = str(item.get("option_type", "")).upper()
            if kind not in {"PUT", "CALL"}:
                continue
            if str(item.get("strike_time", ""))[:10] != expiry.isoformat():
                continue
            quote = item.get("dynamic_quote", {})
            strike = number(item.get("strike_price"))
            bid = field(quote, "bid_price")
            size = field(quote, "contract_size") or number(item.get("lot_size"))
            premium = bid * size if bid is not None and size is not None and size > 0 else None
            delta = field(quote, "delta")
            iv = field(quote, "implied_volatility")
            probability = number(item.get("analytics", {}).get("probability", {}).get("fields", {}).get("strike_probability", {}).get("value"))
            dte = (expiry - date.fromisoformat(selection["analysis_date"])).days
            matches = premium is not None and premium_min <= premium <= premium_max
            cost = None
            if kind == "CALL":
                available = holdings.get(symbol, [])
                if not available:
                    continue
                cost = max((row["cost"] for row in available if row["cost"] is not None), default=None)
            break_even = (strike - bid if kind == "PUT" else cost - bid if cost is not None else None) if strike is not None and bid is not None else None
            base = (strike if kind == "PUT" else cost)
            annual = bid / base * Decimal(365) / Decimal(dte) * Decimal(100) if bid is not None and base and dte > 0 else None
            risks = list(event_risks)
            if symbol_row.get("market_state", {}).get("market_us") not in {"MORNING", "AFTERNOON"}:
                risks.append("报价不在美股正常交易时段，请核对可成交价格")
            if item.get("contract_identity_status") != "ok":
                risks.append("合约乘数或交割方式未完全核实")
            freshness = quote.get("quote_freshness_status", {}).get("value")
            if freshness not in (None, "fresh"):
                risks.append("期权报价时间待核对")
            if premium is None:
                risks.append("可卖报价或合约乘数缺失")
            elif kind == "PUT" and not matches:
                risks.append("权利金不在偏好范围")
            if probability is None:
                risks.append("预计到期价内概率缺失")
            elif not 0 <= probability <= 100:
                probability = None
                risks.append("预计到期价内概率无效")
            if kind == "CALL" and (cost is None or strike is None):
                risks.append("持股成本或行权价待核对")
            elif kind == "CALL" and strike < cost:
                risks.append("Call 行权价低于已录入持股成本")
            if stock_percentile is None:
                risks.append("标的 IV 百分位未取得")
            if stock_percentile is None:
                iv_comment = "标的 IV 历史位置未知，无法比较波动率溢价。"
            elif stock_percentile >= 80:
                iv_comment = "标的 IV 百分位偏高：权利金可能较高，市场预期波动也较大。"
            elif stock_percentile <= 20:
                iv_comment = "标的 IV 百分位偏低：权利金可能偏少，仍需结合合约报价判断。"
            else:
                iv_comment = "标的 IV 百分位处于中间区间，权利金仍以该合约 Bid 为准。"
            if not risks:
                risks.append("请结合报价、行情与持仓自行判断")
            result = {
                "symbol": symbol, "code": str(item.get("code") or ""), "strategy": kind,
                "expiration": expiry.isoformat(), "strike": text(strike), "premium": text(premium),
                "break_even": text(break_even), "delta": text(delta, 4), "iv": text(iv),
                "contract_iv_percentile": None, "underlying_iv_percentile": text(stock_percentile),
                "probability": text(probability), "annualized_premium_rate": text(annual),
                "risks": risks, "premium_match": matches, "quote_source": "Futu Bid",
                "analysis": iv_comment,
                "iv_percentile_source": overview.get("source") if stock_percentile is not None else None,
            }
            results.append(result)
    def sort_key(row):
        boundary = any("不符合本次边界" in reason for reason in row["risks"])
        probability = number(row["probability"])
        return (
            row["strategy"] != "PUT", boundary, not row["premium_match"],
            probability if probability is not None else Decimal(101), row["symbol"],
        )
    return sorted(results, key=sort_key)


def compare_close_rows(report, selection, watch_events, holdings=None):
    """Historical trade closes are observations, never executable sell quotes."""
    expiry = date.fromisoformat(selection["target_expiration"])
    reference = date.fromisoformat(report["reference_date"])
    minimum, maximum = number(selection["premium_min"]), number(selection["premium_max"])
    results = []
    holdings = holdings or {}
    for symbol_row in report["symbols"]:
        symbol = symbol_row["symbol"]
        watch = watch_events.get(symbol)
        for contract in symbol_row["contracts"]:
            kind = contract.get("strategy", "PUT")
            close = number(contract["close"])
            strike = number(contract["strike"])
            premium = close * Decimal(contract["size"]) if close is not None else None
            probability = number(contract["probability"])
            iv = number(contract["iv"])
            cost = max((row["cost"] for row in holdings.get(symbol, []) if row["cost"] is not None), default=None)
            risks = ["历史收盘成交价仅供比较，当前卖出 Bid 和可成交权利金未知"]
            risks.extend(contract["issues"])
            if watch is None or watch.events_checked_at is None or watch.events_covered_until is None or watch.events_covered_until < expiry:
                risks.append("财报及除息日期未核实")
            else:
                if watch.next_earnings and reference <= watch.next_earnings <= expiry:
                    risks.append("到期前跨财报" + ("" if selection["allow_earnings"] else "（不符合本次边界）"))
                if watch.next_dividend and reference <= watch.next_dividend <= expiry:
                    risks.append("到期前跨除息日" + ("" if selection["allow_dividend"] else "（不符合本次边界）"))
            matches = kind == "CALL" or (premium is not None and minimum <= premium <= maximum)
            if kind == "PUT" and premium is not None and not matches:
                risks.append("历史收盘参考金额不在权利金偏好范围")
            if kind == "CALL" and (cost is None or strike is None):
                risks.append("持股成本或 Call 行权价待核对")
            elif kind == "CALL" and strike < cost:
                risks.append("Call 行权价低于已录入持股成本")
            if probability is None:
                risks.append("目标交易日预计到期价内概率缺失")
            if iv is None:
                risks.append("目标交易日合约 IV 缺失")
            results.append({
                "symbol": symbol, "code": contract["code"], "strategy": kind,
                "expiration": expiry.isoformat(), "reference_date": reference.isoformat(),
                "price_basis": "Futu 历史期权日线收盘成交价", "strike": text(strike),
                "premium": text(premium), "break_even": text(strike - close if kind == "PUT" else cost - close)
                if close is not None and (strike is not None if kind == "PUT" else cost is not None) else None,
                "delta": None, "iv": text(iv), "contract_iv_percentile": None,
                "underlying_iv_percentile": None, "probability": text(probability),
                "annualized_premium_rate": None, "premium_match": matches,
                "risks": risks, "analysis": "按上一完整交易日的成交收盘价观察；开盘前请重新核对卖出报价。",
            })
    return sorted(results, key=lambda row: (
        row["strategy"] != "PUT",
        any("不符合本次边界" in risk for risk in row["risks"]),
        not row["premium_match"],
        number(row["probability"]) if row["probability"] is not None else Decimal(101),
        row["symbol"],
    ))
