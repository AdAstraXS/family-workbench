"""Previous completed US session option observations for the household screener.

Only historical daily data is used. No subscriptions or trading API calls.
"""

from datetime import date, datetime, timedelta
from decimal import Decimal
import json
import os
import subprocess
import sys
import time

from portfolio.futu_option_probe import _is_standard_contract, _normalize_bool

from .close_data import CloseDataError, NY, calendar_target, daily_row, number

FRAME = "WHEEL_SCREEN_CLOSE:"


def _call(context, method, **kwargs):
    try:
        response = getattr(context, method)(**kwargs)
        if not isinstance(response, tuple) or len(response) < 2:
            raise CloseDataError(f"{method} 响应格式无效")
        if response[0] != 0:
            # Provider text can include account details; only expose known categories.
            message = str(response[1]).lower()
            if any(word in message for word in ("timeout", "timed out", "超时")):
                category = "查询超时"
            elif any(word in message for word in ("frequency", "too frequent", "rate limit", "频率", "过于频繁")):
                category = "请求频率限制"
            elif any(word in message for word in ("permission", "privilege", "no right", "权限")):
                category = "行情权限不足"
            elif any(word in message for word in ("quota", "额度")):
                category = "行情额度不足"
            else:
                category = "供应商拒绝请求"
            if method == "get_option_chain" and category in {"查询超时", "请求频率限制"}:
                time.sleep(31 if category == "请求频率限制" else 1)
                return _call_once(context, method, kwargs)
            raise CloseDataError(f"{method}：{category}")
        if method == "request_history_kline" and len(response) > 2 and response[2] is not None:
            raise CloseDataError(f"{method} 历史数据未取全")
        data = response[1]
        return data.to_dict("records") if hasattr(data, "to_dict") else data
    except CloseDataError:
        raise
    except Exception:
        raise CloseDataError(f"{method} 查询失败") from None


def _call_once(context, method, kwargs):
    """A single bounded retry; never retry permissions or arbitrary provider errors."""
    try:
        response = getattr(context, method)(**kwargs)
        if not isinstance(response, tuple) or len(response) < 2 or response[0] != 0:
            raise CloseDataError(f"{method}：重试后仍未返回数据")
        data = response[1]
        return data.to_dict("records") if hasattr(data, "to_dict") else data
    except CloseDataError:
        raise
    except Exception:
        raise CloseDataError(f"{method}：重试失败") from None


def _positive(value):
    result = number(value)
    return result if result is not None and result > 0 else None


def _contract_identity(row, symbol, expiry, kind):
    return (
        row.get("stock_owner") == "US." + symbol
        and row.get("option_type") == kind
        and str(row.get("strike_time", ""))[:10] == expiry.isoformat()
        and str(row.get("code", "")).startswith("US." + symbol)
        and _is_standard_contract(row)
        and number(row.get("lot_size")) == Decimal(100)
        and _normalize_bool(row.get("suspension")) is False
        and _positive(row.get("strike_price")) is not None
    )


def collect(context, symbols, expiry, now, calls_for=()):
    query_day = now.astimezone(NY).date()
    calendar = _call(context, "request_trading_days", code="US." + symbols[0],
                     start=str(query_day - timedelta(days=20)), end=str(query_day + timedelta(days=14)))
    target, _, _ = calendar_target(calendar, now)
    result = {"reference_date": target.isoformat(), "symbols": [], "issues": []}
    for symbol in symbols:
        code = "US." + symbol
        item = {"symbol": symbol, "contracts": [], "issues": []}
        result["symbols"].append(item)
        try:
            stock_rows = _call(context, "request_history_kline", code=code, start=str(target), end=str(target),
                               ktype="K_DAY", autype="None", max_count=10)
            stock = daily_row(stock_rows, target, code=code)
            spot = _positive(stock.get("close")) if stock else None
            if spot is None:
                raise CloseDataError("目标交易日正股收盘价缺失")
            item["stock_close"] = str(spot)
            try:
                overview = _call(context, "get_option_underlying_overview", code_list=[code])
                matching = [row for row in overview if row.get("code") == code]
                percentile = number(matching[0].get("iv_percentile")) if matching else None
                if percentile is not None and 0 <= percentile <= 100:
                    item["underlying_iv_percentile"] = str(percentile)
                    item["underlying_iv_queried_at"] = datetime.now(NY).isoformat(timespec="seconds")
            except CloseDataError:
                pass  # Latest overview is optional and never blocks a historical chain.
            chain = _call(context, "get_option_chain", code=code, start=str(expiry), end=str(expiry), option_type="PUT")
            eligible = [row for row in chain if _contract_identity(row, symbol, expiry, "PUT")]
            eligible.sort(key=lambda row: (abs(number(row["strike_price"]) - spot), row["code"]))
            item["chain_count"] = len(chain)
            selected = eligible[:8]
            if not eligible:
                item["issues"].append("所选到期日没有可确认的标准 Put 合约")
            if symbol in calls_for:
                try:
                    calls = _call(context, "get_option_chain", code=code, start=str(expiry), end=str(expiry), option_type="CALL")
                    call_options = [row for row in calls if _contract_identity(row, symbol, expiry, "CALL")]
                    call_options.sort(key=lambda row: (number(row["strike_price"]) < spot,
                                                       abs(number(row["strike_price"]) - spot), row["code"]))
                    selected.extend(call_options[:1])
                    if not call_options:
                        item["issues"].append("所选到期日没有可确认的标准 Call 合约")
                except CloseDataError as exc:
                    item["issues"].append("Call 链：" + str(exc))
            item["sampled_count"] = len(selected)
            for row in selected:
                contract = {
                    "code": str(row["code"]), "strike": str(number(row["strike_price"])),
                    "strategy": row["option_type"], "size": 100, "close": None, "iv": None, "probability": None,
                    "issues": [],
                }
                item["contracts"].append(contract)
                try:
                    history = _call(context, "request_history_kline", code=row["code"],
                                    start=str(target), end=str(target), ktype="K_DAY", autype="None", max_count=10)
                    daily = daily_row(history, target, code=row["code"])
                    close = _positive(daily.get("close")) if daily else None
                    volume = _positive(daily.get("volume")) if daily else None
                    if close is None or volume is None:
                        contract["issues"].append("目标日无有效期权成交收盘价")
                    else:
                        contract["close"] = str(close)
                except CloseDataError as exc:
                    contract["issues"].append(str(exc))
                for method, key, source in (
                    ("get_option_volatility", "iv", "implied_volatility"),
                    ("get_option_exercise_probability", "probability", "strike_probability"),
                ):
                    try:
                        records = _call(context, method, code=row["code"])
                        daily = daily_row(records, target, analytics=True)
                        value = number(daily.get(source)) if daily else None
                        if value is None or value < 0 or (key == "probability" and value > 100):
                            raise CloseDataError(f"{method} 目标日数值缺失")
                        contract[key] = str(value)
                    except CloseDataError as exc:
                        contract["issues"].append(str(exc))
        except CloseDataError as exc:
            item["issues"].append(str(exc))
    return result


def fetch(symbols, expiry, calls_for=()):
    """Run the SDK outside the web/job process and keep its response bounded."""
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "option_wheel.screen_close", str(expiry),
             "--calls-for=" + ",".join(sorted(calls_for)), *symbols],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=420, check=False,
        )
        frames = [line[len(FRAME):] for line in completed.stdout.splitlines() if line.startswith(FRAME)]
        if completed.returncode or len(frames) != 1:
            raise CloseDataError("Futu 收盘数据查询进程未完成")
        result = json.loads(frames[0])
        if not isinstance(result, dict) or [row.get("symbol") for row in result.get("symbols", [])] != symbols:
            raise CloseDataError("Futu 收盘数据响应不完整")
        return result
    except subprocess.TimeoutExpired:
        raise CloseDataError("Futu 收盘数据查询超过 420 秒") from None
    except (OSError, ValueError, TypeError):
        raise CloseDataError("Futu 收盘数据响应无效") from None


if __name__ == "__main__":
    context = None
    try:
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
        import django
        django.setup()
        from django.conf import settings
        from futu import OpenQuoteContext

        expiry_arg = date.fromisoformat(sys.argv[1])
        calls_for = set(sys.argv[2].removeprefix("--calls-for=").split(",")) if sys.argv[2].startswith("--calls-for=") else set()
        symbol_args = sys.argv[3:]
        if not symbol_args or len(symbol_args) > 20 or any(not s.isalnum() for s in symbol_args):
            raise ValueError("invalid symbols")
        context = OpenQuoteContext(host=settings.FUTU_OPEND_HOST, port=settings.FUTU_OPEND_PORT)
        payload = collect(context, symbol_args, expiry_arg, datetime.now(NY), calls_for & set(symbol_args))
        output = json.dumps(payload, ensure_ascii=True)
    except Exception:
        sys.exit(1)
    finally:
        if context is not None:
            context.close()
    print("\n" + FRAME + output, flush=True)
