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


def present_results(rows, selection):
    """Apply the saved Put preference to old and new observations alike."""
    minimum, maximum = number(selection.get("premium_min")), number(selection.get("premium_max"))
    visible = []
    for original in rows:
        row = dict(original)
        premium = number(row.get("premium"))
        if row.get("strategy") == "PUT" and (
            premium is None or minimum is None or maximum is None or not minimum <= premium <= maximum
        ):
            continue
        row["risks"] = [risk for risk in row.get("risks", [])
                        if risk != "标的 IV 百分位是 Futu 最新查询值，并非历史收盘日数值"]
        visible.append(row)
    visible.sort(key=lambda row: (
        row.get("symbol") or "",
        number(row.get("probability")) if number(row.get("probability")) is not None else Decimal(101),
        row.get("code") or "",
    ))
    lowest_put = {}
    for row in visible:
        if row.get("strategy") == "PUT" and number(row.get("probability")) is not None:
            lowest_put.setdefault(row["symbol"], row)
    for row in visible:
        probability = number(row.get("probability"))
        if row.get("strategy") == "PUT" and probability is not None:
            best = lowest_put.get(row["symbol"])
            if probability == number(best["probability"]):
                row["display_analysis"] = "本标的已取得且符合权利金范围的 Put 中，预计到期价内概率最低；仍须核对事件及报价风险。"
            else:
                gap = probability - number(best["probability"])
                premium_gain = number(row.get("premium")) - number(best.get("premium"))
                if premium_gain > 0:
                    row["display_analysis"] = (f"到期价内概率比本标的最低值高 {text(gap)} 个百分点，"
                                               f"权利金多 ${text(premium_gain)}；请权衡增收与行权风险。")
                else:
                    row["display_analysis"] = (f"到期价内概率比本标的最低值高 {text(gap)} 个百分点，"
                                               "权利金未更高；可优先比较同标的较低概率合约。")
        elif row.get("strategy") == "PUT":
            row["display_analysis"] = "缺少可比的到期价内概率；请结合权利金、行权价和事件风险自行判断。"
        else:
            strike, cost = number(row.get("strike")), number(row.get("cost"))
            if strike is not None and cost is not None:
                difference = strike - cost
                if difference >= 0:
                    row["display_analysis"] = (f"行权价比该账户录入均价高 ${text(difference)}；"
                                               "请结合权利金与到期价内概率权衡正股被卖出的可能。")
                else:
                    row["display_analysis"] = (f"行权价比该账户录入均价低 ${text(-difference)}；"
                                               "如被行权，正股会按此行权价卖出。")
            else:
                row["display_analysis"] = "Covered Call 请比较行权价与该账户持股成本，以及正股被行权卖出的风险。"
    return visible


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
                continue
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
                "underlying_iv_percentile": text(stock_percentile),
                "probability": text(probability), "annualized_premium_rate": text(annual),
                "risks": risks, "premium_match": matches, "quote_source": "Futu Bid",
                "analysis": iv_comment,
                "iv_percentile_source": overview.get("source") if stock_percentile is not None else None,
            }
            if kind == "CALL":
                for holding in available:
                    account_cost = holding["cost"]
                    account_row = dict(result)
                    account_row["account"] = holding["account"]
                    account_row["shares"] = text(holding["shares"], 0)
                    account_row["cost"] = text(account_cost)
                    account_row["break_even"] = text(account_cost - bid) if account_cost is not None and bid is not None else None
                    account_row["annualized_premium_rate"] = text(
                        bid / account_cost * Decimal(365) / Decimal(dte) * Decimal(100)
                    ) if bid is not None and account_cost is not None and account_cost > 0 and dte > 0 else None
                    account_row["risks"] = [risk for risk in risks if risk not in (
                        "持股成本或行权价待核对", "Call 行权价低于已录入持股成本")]
                    if account_cost is None or strike is None:
                        account_row["risks"].append("持股成本或行权价待核对")
                    elif strike < account_cost:
                        account_row["risks"].append("Call 行权价低于该账户持股成本")
                    results.append(account_row)
            else:
                results.append(result)
    return present_results(results, selection)


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
        stock_percentile = number(symbol_row.get("underlying_iv_percentile"))
        if stock_percentile is not None and not 0 <= stock_percentile <= 100:
            stock_percentile = None
        queried_at = symbol_row.get("underlying_iv_queried_at") if stock_percentile is not None else None
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
            if kind == "PUT" and not matches:
                continue
            if kind == "CALL" and (cost is None or strike is None):
                risks.append("持股成本或 Call 行权价待核对")
            elif kind == "CALL" and strike < cost:
                risks.append("Call 行权价低于已录入持股成本")
            if probability is None:
                risks.append("目标交易日预计到期价内概率缺失")
            if iv is None:
                risks.append("目标交易日合约 IV 缺失")
            dte = (expiry - reference).days
            base = strike if kind == "PUT" else cost
            annual = (close / base * Decimal(365) / Decimal(dte) * Decimal(100)
                      if close is not None and base is not None and base > 0 and dte > 0 else None)
            result = {
                "symbol": symbol, "code": contract["code"], "strategy": kind,
                "expiration": expiry.isoformat(), "reference_date": reference.isoformat(),
                "price_basis": "Futu 历史期权日线收盘成交价", "strike": text(strike),
                "premium": text(premium), "break_even": text(strike - close if kind == "PUT" else cost - close)
                if close is not None and (strike is not None if kind == "PUT" else cost is not None) else None,
                "delta": None, "iv": text(iv), "underlying_iv_percentile": text(stock_percentile),
                "underlying_iv_queried_at": queried_at, "probability": text(probability),
                "annualized_premium_rate": text(annual), "premium_match": matches,
                "risks": risks, "analysis": "按上一完整交易日的成交收盘价观察；开盘前请重新核对卖出报价。",
            }
            if kind == "CALL":
                for holding in holdings.get(symbol, []):
                    account_cost = holding["cost"]
                    account_row = dict(result)
                    account_row["account"] = holding["account"]
                    account_row["shares"] = text(holding["shares"], 0)
                    account_row["cost"] = text(account_cost)
                    account_row["break_even"] = text(account_cost - close) if account_cost is not None and close is not None else None
                    account_row["annualized_premium_rate"] = text(
                        close / account_cost * Decimal(365) / Decimal(dte) * Decimal(100)
                    ) if close is not None and account_cost is not None and account_cost > 0 and dte > 0 else None
                    account_row["risks"] = [risk for risk in risks if risk not in (
                        "持股成本或 Call 行权价待核对", "Call 行权价低于已录入持股成本")]
                    if account_cost is None or strike is None:
                        account_row["risks"].append("持股成本或 Call 行权价待核对")
                    elif strike < account_cost:
                        account_row["risks"].append("Call 行权价低于该账户持股成本")
                    results.append(account_row)
            else:
                results.append(result)
    return present_results(results, selection)
