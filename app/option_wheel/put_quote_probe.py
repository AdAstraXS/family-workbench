"""Bounded Futu quote read for exact portfolio Put contracts."""

import re
import time
from decimal import Decimal, InvalidOperation

from django.conf import settings

from portfolio.futu_option_probe import (
    MIN_SUBSCRIPTION_SECONDS, ProbeLock, records_from, sdk_call,
    subscription_summary, unsubscribe_owned, verify_subscription_restored,
)


CODE = re.compile(r"^US\.[A-Z0-9]+$")


class PutQuoteError(ValueError):
    pass


def amount(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return str(result) if result.is_finite() and result >= 0 else None


def quote_code(contract):
    if contract.is_adjusted or contract.multiplier != 100 or contract.underlying.market != "US":
        return None
    if contract.provider == "futu" and contract.provider_contract_code:
        code = contract.provider_contract_code.strip().upper()
        return code if CODE.fullmatch(code) else None
    root = contract.underlying.symbol.strip().upper()
    strike = contract.strike_price * 1000
    if not re.fullmatch(r"[A-Z]+", root) or strike != strike.to_integral_value() or strike <= 0:
        return None
    option_type = "P" if contract.option_type == "put" else "C"
    return f"US.{root}{contract.expiration_date:%y%m%d}{option_type}{int(strike)}"


def fetch_exact_option_quotes(codes):
    """Subscribe only requested contracts and verify all subscription counts return."""
    codes = sorted(set(codes))
    if not codes or len(codes) > 20 or any(not CODE.fullmatch(code) for code in codes):
        raise PutQuoteError("期权合约代码缺失或超过单次 20 张上限。")
    lock = ProbeLock()
    if not lock.acquire():
        raise PutQuoteError("已有 Futu 期权查询正在运行；本次没有订阅。")
    context = None
    owned = []
    started = {}
    before = None
    quotes = {}
    query_error = None
    cleanup_error = None
    try:
        from futu import OpenQuoteContext, RET_OK, SubType

        context = OpenQuoteContext(host=settings.FUTU_OPEND_HOST, port=settings.FUTU_OPEND_PORT)
        response = sdk_call(context, "query_subscription", RET_OK, False)
        before = subscription_summary(response["data"]) if response["status"] == "ok" else None
        if not before or before["data_status"] != "ok":
            raise PutQuoteError("订阅前额度状态不完整，未开始查询。")
        existing = set(before["existing_quote_codes"])
        needed = [code for code in codes if code not in existing]
        if (Decimal(str(before["remain"])) < len(needed)
                or Decimal(str(before["option_remain_quota"])) < len(needed)):
            raise PutQuoteError("Futu 剩余动态订阅额度不足，未开始查询。")
        for code in codes:
            if code not in existing:
                started[code] = time.monotonic()
                owned.append(code)
                response = sdk_call(context, "subscribe", RET_OK, [code], [SubType.QUOTE], subscribe_push=False)
                if response["status"] != "ok":
                    raise PutQuoteError("Futu 未能订阅全部持仓合约。")
            snapshot = sdk_call(context, "get_market_snapshot", RET_OK, [code])
            rows = records_from(snapshot["data"]) if snapshot["status"] == "ok" else []
            if not rows:
                quotes[code] = {"error": "Futu 未返回该合约报价"}
                continue
            row = rows[0]
            quote = sdk_call(context, "get_stock_quote", RET_OK, [code])
            quote_rows = records_from(quote["data"]) if quote["status"] == "ok" else []
            detail = quote_rows[0] if quote_rows else {}
            probability_response = sdk_call(context, "get_option_exercise_probability", RET_OK, code)
            probability_rows = records_from(probability_response["data"]) if probability_response["status"] == "ok" else []
            probability = probability_rows[0].get("strike_probability") if probability_rows else None
            quotes[code] = {
                "bid": amount(row.get("bid_price")), "ask": amount(row.get("ask_price")),
                "last": amount(detail.get("last_price")),
                "iv": amount(detail.get("implied_volatility")),
                "delta": str(detail.get("delta")) if detail.get("delta") is not None else None,
                "probability": amount(probability),
                "as_of": str(row.get("update_time") or ""),
                "source": "Futu get_market_snapshot / get_stock_quote / get_option_exercise_probability",
            }
    except PutQuoteError as exc:
        query_error = str(exc)
    except Exception:
        query_error = "Futu 持仓报价查询异常。"
    finally:
        if context is not None:
            try:
                if started:
                    remaining = max(started.values()) + MIN_SUBSCRIPTION_SECONDS - time.monotonic()
                    if remaining > 0:
                        time.sleep(remaining)
                from futu import RET_OK, SubType
                if unsubscribe_owned(context, RET_OK, SubType, owned) not in ("restored", "not_requested"):
                    cleanup_error = "临时订阅释放未确认。"
                after_response = sdk_call(context, "query_subscription", RET_OK, False)
                after = subscription_summary(after_response["data"]) if after_response["status"] == "ok" else None
                if before and after and verify_subscription_restored(before, after, owned)["status"] != "restored":
                    cleanup_error = "订阅前后额度或代码未恢复。"
                elif before and after is None:
                    cleanup_error = "订阅清理后额度未能核对。"
            except Exception:
                cleanup_error = "订阅清理状态未知。"
            finally:
                try:
                    context.close()
                except Exception:
                    cleanup_error = "Futu 连接关闭状态未知。"
        lock.release()
    if cleanup_error:
        raise PutQuoteError(cleanup_error)
    if query_error:
        raise PutQuoteError(query_error)
    return quotes


# Keep the existing Put quote job interface stable.
fetch_exact_put_quotes = fetch_exact_option_quotes
