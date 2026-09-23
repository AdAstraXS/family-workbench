"""Bounded Futu comparison for one recorded option and one chosen expiration."""

from decimal import Decimal, InvalidOperation

from django.conf import settings

from portfolio.futu_option_probe import _is_standard_contract, records_from, sdk_call_with_timeout_retry

from .probe_diagnostics import _issue_text
from .put_quote_probe import CODE, PutQuoteError, fetch_exact_option_quotes, quote_code


def _number(value):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return number if number.is_finite() else None


def select_chain_rows(rows, contract, side, limit=8):
    """Sample nearby strikes; a finite subset is never described as the full chain."""
    wanted = contract.option_type.upper()
    selected = []
    for row in rows:
        strike = _number(row.get("strike_price"))
        code = str(row.get("code") or "")
        lot = _number(row.get("lot_size"))
        if (str(row.get("option_type", "")).upper() != wanted or strike is None
                or lot != Decimal(100) or not CODE.fullmatch(code)
                or not _is_standard_contract(row)):
            continue
        if side == "short_put" and strike > contract.strike_price:
            continue
        if side == "short_call" and strike < contract.strike_price:
            continue
        selected.append((strike, code))
    selected.sort(key=lambda item: (abs(item[0] - contract.strike_price), item[0], item[1]))
    return [{"strike": str(strike), "code": code} for strike, code in selected[:limit]]


def fetch_position_scan(position, target_expiration):
    contract = position.security.option_contract
    old_code = quote_code(contract)
    if old_code is None:
        raise PutQuoteError("旧持仓不是可识别的标准 100 股美股合约，未查询行情。")
    symbol = f"US.{contract.underlying.symbol.upper()}"
    context = None
    try:
        from futu import OpenQuoteContext, OptionType, RET_OK

        context = OpenQuoteContext(host=settings.FUTU_OPEND_HOST, port=settings.FUTU_OPEND_PORT)
        response = sdk_call_with_timeout_retry(
            context, "get_option_chain", RET_OK, symbol,
            start=target_expiration.isoformat(), end=target_expiration.isoformat(),
            option_type=OptionType.PUT if contract.option_type == "put" else OptionType.CALL,
        )
        if response["status"] != "ok":
            raise PutQuoteError("Futu " + _issue_text({
                "source": "chain", "category": response.get("category"),
                "error": response.get("error"),
            }))
        rows = [row for row in records_from(response["data"])
                if str(row.get("strike_time") or row.get("expiration_date") or "")[:10]
                == target_expiration.isoformat()]
    except PutQuoteError:
        raise
    except Exception:
        raise PutQuoteError("Futu 期权链查询异常。") from None
    finally:
        if context is not None:
            context.close()
    side = "short_put" if position.quantity < 0 and contract.option_type == "put" else (
        "short_call" if position.quantity < 0 else "long"
    )
    candidates = select_chain_rows(rows, contract, side)
    codes = [old_code] + [row["code"] for row in candidates if row["code"] != old_code]
    if len(codes) > 20:
        raise PutQuoteError("本次查询超过 20 张合约上限。")
    quotes = fetch_exact_option_quotes(codes)
    return {
        "old_code": old_code, "old_quote": quotes.get(old_code, {}),
        "candidates": [{**row, "quote": quotes.get(row["code"], {})} for row in candidates],
        "chain_count": len(rows), "sample_count": len(candidates),
        "target_expiration": target_expiration.isoformat(),
        "position_quantity": str(position.quantity), "position_avg_cost": str(position.avg_cost),
        "position_date": position.position_date.isoformat(),
    }


def comparison_rows(position_row, result):
    """Per-contract cash figures; never infer a missing bid or ask."""
    if not result:
        return {"old_close": None, "old_pnl": None, "rows": []}
    multiplier = Decimal(position_row["contract"].multiplier)
    short = position_row["position"].quantity < 0
    old_quote = result.get("old_quote") or {}
    old_price = _number(old_quote.get("ask" if short else "bid"))
    old_close = (old_price * multiplier).quantize(Decimal("0.01")) if old_price is not None and old_price > 0 else None
    opening = position_row["open_per_contract"]
    old_pnl = ((opening - old_close) if short else (old_close - opening)) if old_close is not None and opening is not None else None
    if old_pnl is not None:
        old_pnl = old_pnl.quantize(Decimal("0.01"))
    rows = []
    for item in result.get("candidates", []):
        quote = item.get("quote") or {}
        strike = _number(item["strike"])
        new_price = _number(quote.get("bid" if short else "ask"))
        new_open = (new_price * multiplier).quantize(Decimal("0.01")) if new_price is not None and new_price > 0 else None
        net = ((new_open - old_close) if short else (old_close - new_open)) if old_close is not None and new_open is not None else None
        if strike is None:
            analysis = "行权价未知，待核对合约。"
        elif strike == position_row["contract"].strike_price:
            analysis = "行权价不变；比较新增期限与换仓净收支。"
        elif position_row["purpose"] == "protective_put":
            analysis = "保护价格提高，成本可能增加。" if strike > position_row["contract"].strike_price else "保护价格降低，正股下跌保护减弱。"
        elif position_row["contract"].option_type == "call" and not short:
            analysis = "行权价提高，到期盈利门槛提高。" if strike > position_row["contract"].strike_price else "行权价降低，核对新增成本。"
        else:
            analysis = "行权价提高，核对指派风险。" if strike > position_row["contract"].strike_price else "行权价降低，仍需承担继续下跌风险。"
        rows.append({
            "code": item["code"], "strike": strike, "analysis": analysis,
            "old_close": old_close, "old_pnl": old_pnl, "new_open": new_open,
            "net": net, "net_abs": abs(net) if net is not None else None,
            "iv": quote.get("iv"), "delta": quote.get("delta"),
            "probability": quote.get("probability"), "as_of": quote.get("as_of"),
        })
    return {"old_close": old_close, "old_pnl": old_pnl, "rows": rows,
            "old_quote_as_of": old_quote.get("as_of")}
