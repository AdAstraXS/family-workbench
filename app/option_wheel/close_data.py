"""Daily evidence only. No subscriptions, trading APIs, database writes or live quotes."""

from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
import json
import os
import subprocess
import sys
from zoneinfo import ZoneInfo

from portfolio.futu_option_probe import _is_standard_contract, _normalize_bool

NY = ZoneInfo("America/New_York")
SYMBOLS = ("TSLA", "MSFT", "NVDA")
MODE = "daily-close-observation-v1"
CHILD_ERRORS = {
    "setup": "查询进程初始化失败",
    "connect": "行情连接建立失败",
    "collect": "日历或历史数据采集失败",
    "serialize": "日级证据序列化失败",
    "close": "行情连接关闭失败",
    "calendar_query": "交易日历接口未成功",
    "calendar_data": "交易日历日期、类型或覆盖范围无法确认",
}


class CloseDataError(ValueError):
    pass


def number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return result if result.is_finite() else None


def calendar_target(rows, now):
    """Conservative 30-minute publication buffer, including US half sessions."""
    if now.tzinfo is None:
        raise CloseDataError("采集时间缺少时区。")
    sessions = {}
    for row in rows:
        try:
            day = date.fromisoformat(row["time"])
            kind = row["trade_date_type"]
            hour = {"WHOLE": 16, "HALF": 13}[kind]
        except (KeyError, TypeError, ValueError):
            raise CloseDataError("交易日历类型或日期未知，不能推定上一交易日。") from None
        if day in sessions:
            raise CloseDataError("交易日历存在重复日期。")
        sessions[day] = (kind, datetime.combine(day, time(hour, 30), NY))
    eligible = [day for day, (_, ready_at) in sessions.items() if ready_at <= now]
    if not eligible:
        raise CloseDataError("没有已结束且经过发布缓冲期的交易日。")
    target = max(eligible)
    following = sorted(day for day in sessions if day > target)
    if not following or (now.astimezone(NY).date() - target).days > 10:
        raise CloseDataError("交易日历覆盖不足。")
    return target, following[0], sessions[target][0]


def daily_row(rows, target, *, code=None, analytics=False):
    """Strict target-date match; never forward-fill or choose the last row."""
    matches = []
    for row in rows:
        try:
            stamp = datetime.fromisoformat(str(row.get("timestamp_str" if analytics else "time_key", "")))
            day = stamp.astimezone(NY).date() if stamp.tzinfo else stamp.date()
        except (TypeError, ValueError):
            continue
        if day != target:
            continue
        if code is not None and row.get("code") != code:
            continue
        if analytics:
            try:
                unix_day = datetime.fromtimestamp(int(row["timestamp"]), NY).date()
            except (KeyError, ValueError, TypeError, OSError, OverflowError):
                return None
            if unix_day != target:
                return None
        matches.append(row)
    return matches[0] if len(matches) == 1 else None


def technical_summary(rows, target, code, session_days=None):
    cleaned = {}
    for row in rows:
        try:
            day = date.fromisoformat(str(row.get("time_key", ""))[:10])
        except ValueError:
            continue
        if day > target or row.get("code") != code:
            continue
        close = number(row.get("close"))
        if day in cleaned or close is None or close <= 0:
            return {"status": "日线重复或价格无效"}
        cleaned[day] = close
    if target not in cleaned or len(cleaned) < 50:
        return {"status": "截至目标日的有效日线不足 50 条"}
    if session_days is not None:
        expected = sorted(day for day in session_days if day <= target)[-50:]
        if len(expected) < 50 or any(day not in cleaned for day in expected):
            return {"status": "最近 50 个日历交易日有缺失，不跨缺口计算均线"}
        cleaned = {day: cleaned[day] for day in expected}
    values = [cleaned[day] for day in sorted(cleaned)]
    return {
        "status": "日线参考（不复权）", "sample_count": len(values),
        "sma20": str((sum(values[-20:]) / Decimal(20)).quantize(Decimal("0.0001"))),
        "sma50": str((sum(values[-50:]) / Decimal(50)).quantize(Decimal("0.0001"))),
        "note": "不复权序列遇拆股、分红可能跳变；均线仅作参考，不产生买卖信号。",
    }


def contract_reasons(row, code, reference_day, spot):
    reasons = []
    if row.get("stock_owner") != code or row.get("option_type") != "PUT":
        reasons.append("标的或 Put 身份不匹配")
    if not _is_standard_contract(row) or number(row.get("lot_size")) != 100:
        reasons.append("非标准合约或 100 股乘数未证实")
    if _normalize_bool(row.get("suspension")) is not False:
        reasons.append("停牌状态未知或已停牌")
    strike = number(row.get("strike_price"))
    if strike is None or strike <= 0 or strike >= spot:
        reasons.append("不是相对目标日收盘价的价外 Put")
    try:
        expiry = date.fromisoformat(row.get("strike_time", ""))
        if not 4 <= (expiry - reference_day).days <= 9:
            reasons.append("不在本次采样的 4–9 DTE 范围")
    except (TypeError, ValueError):
        reasons.append("到期日未知")
    if not str(row.get("code", "")).startswith(code):
        reasons.append("合约代码缺失或不匹配")
    return reasons


def collect(context, symbol, now):
    if symbol not in SYMBOLS:
        raise CloseDataError("首版仅支持 TSLA、MSFT、NVDA。")
    code = "US." + symbol
    query_day = now.astimezone(NY).date()

    def call(method, **kwargs):
        try:
            result = getattr(context, method)(**kwargs)
            if result[0] != 0:
                raise CloseDataError(method + " 查询未成功")
            if len(result) > 2 and result[2] is not None:
                raise CloseDataError(method + " 数据未完整分页")
            data = result[1]
            return data.to_dict("records") if hasattr(data, "to_dict") else data
        except CloseDataError:
            raise
        except Exception:
            raise CloseDataError(method + " 查询异常") from None

    calendar = call("request_trading_days", code=code,
                    start=str(query_day - timedelta(days=120)), end=str(query_day + timedelta(days=14)))
    target, next_session, kind = calendar_target(calendar, now)
    report = {
        "mode": MODE, "symbol": symbol, "target_date": str(target),
        "next_session": str(next_session), "collected_at": now.isoformat(),
        "calendar": calendar, "session_type": kind, "provider": "Futu",
        "reference_day": str(query_day), "adjustment": "NONE",
        "candidates": [], "excluded": [], "issues": [],
        "technical": {"status": "未知"}, "stock_close": None,
        "events": "未在此模式重新核验财报/除息，不能形成策略建议",
        "bid_ask": None, "delta": None, "execution_allowed": False,
    }
    try:
        history = call("request_history_kline", code=code, start=str(target - timedelta(days=120)),
                       end=str(target), ktype="K_DAY", autype="None", max_count=200)
        report["stock_history"] = [
            {key: str(row.get(key, "")) for key in ("code", "time_key", "close", "volume")}
            for row in history
        ]
        report["technical"] = technical_summary(history, target, code, [date.fromisoformat(row["time"]) for row in calendar])
        stock = daily_row(history, target, code=code)
        spot = number(stock.get("close")) if stock else None
        volume = number(stock.get("volume")) if stock else None
        if spot is None or spot <= 0 or volume is None or volume <= 0:
            raise CloseDataError("目标日正股行情缺失、重复或无有效成交；不使用前值代替。")
        report["stock_close"] = str(spot)
        chain = call("get_option_chain", code=code, start=str(query_day + timedelta(days=4)),
                     end=str(query_day + timedelta(days=9)), option_type="PUT")
        report["chain_count"] = len(chain)
        eligible = []
        seen = set()
        for row in chain:
            reasons = contract_reasons(row, code, query_day, spot)
            if row.get("code") in seen:
                raise CloseDataError("期权链合约代码重复，本次不选择合约。")
            seen.add(row.get("code"))
            if reasons:
                report["excluded"].append({"code": str(row.get("code", "未知")), "reasons": reasons})
            else:
                eligible.append(row)
        # Bounded coverage sample, NOT a premium/risk recommendation or exhaustive ranking.
        eligible.sort(key=lambda row: (row["strike_time"], -number(row["strike_price"]), row["code"]))
        report["unsampled_count"] = max(0, len(eligible) - 3)
        for row in eligible[:3]:
            item = {key: str(row.get(key, "")) for key in (
                "code", "stock_owner", "strike_time", "strike_price", "lot_size", "option_standard_type",
                "option_settlement_mode", "option_type", "suspension",
            )}
            item.update(close=None, volume=None, probability=None, iv=None, hv=None, reasons=[], status="待开盘核价")
            for method, analytics, fields in (
                ("request_history_kline", False, (("close", "close"), ("volume", "volume"))),
                ("get_option_exercise_probability", True, (("probability", "strike_probability"),)),
                ("get_option_volatility", True, (("iv", "implied_volatility"), ("hv", "history_volatility"))),
            ):
                try:
                    params = {"code": row["code"]}
                    if not analytics:
                        params.update(start=str(target), end=str(target), ktype="K_DAY", autype="None", max_count=10)
                    records = call(method, **params)
                    match = daily_row(records, target, code=None if analytics else row["code"], analytics=analytics)
                    if match is None:
                        raise CloseDataError(method + " 目标日记录缺失、重复或日期不一致")
                    for destination, source in fields:
                        value = number(match.get(source))
                        if value is None or value < 0 or (destination == "probability" and value > 100):
                            raise CloseDataError(method + " 数值无效")
                    item[method] = {key: str(match.get(key, "")) for key in (
                        "code", "time_key", "timestamp", "timestamp_str", "security_price", *[f[1] for f in fields],
                    )}
                    for destination, source in fields:
                        item[destination] = str(number(match[source]))
                except CloseDataError as exc:
                    item["reasons"].append(str(exc))
            if number(item.get("volume")) is None or number(item.get("volume")) <= 0 or number(item["close"]) in (None, Decimal(0)):
                item["reasons"].append("目标日期权无有效成交参考价")
            if item["reasons"]:
                item["status"] = "数据不足，排除"
            report["candidates"].append(item)
        if not eligible:
            report["issues"].append("本次有限采样范围内没有符合条件的合约。")
    except CloseDataError as exc:
        report["issues"].append(str(exc))
    report["finished_at"] = datetime.now(NY).isoformat()
    return report


def fetch_close_report(symbol):
    """Bound the complete SDK process, including connection and shutdown, to 80s."""
    if symbol not in SYMBOLS:
        raise CloseDataError("标的不在首版范围。")
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "option_wheel.close_data", symbol],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=80, check=False,
        )
        errors = [line.removeprefix("WHEEL_CLOSE_ERROR:") for line in completed.stdout.splitlines() if line.startswith("WHEEL_CLOSE_ERROR:")]
        if completed.returncode:
            reason = CHILD_ERRORS.get(errors[-1], "查询进程未正常完成") if errors else "查询进程未正常完成"
            raise CloseDataError(f"{reason}，未保存观察报告。")
        lines = [line.removeprefix("WHEEL_CLOSE:") for line in completed.stdout.splitlines() if line.startswith("WHEEL_CLOSE:")]
        if len(lines) != 1:
            raise CloseDataError("查询进程没有返回唯一报告，未保存观察报告。")
        result = json.loads(lines[0])
        if not isinstance(result, dict) or result.get("mode") != MODE or result.get("symbol") != symbol:
            raise ValueError("invalid result")
        return result
    except CloseDataError:
        raise
    except subprocess.TimeoutExpired:
        raise CloseDataError("收盘查询超过 80 秒，已结束只读查询进程，未保存报告。") from None
    except (OSError, ValueError):
        raise CloseDataError("收盘查询响应无效，未保存观察报告。") from None


if __name__ == "__main__":
    context = None
    stage = "setup"
    error = None
    output = None
    try:
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
        import django
        django.setup()
        from django.conf import settings
        import futu

        stage = "connect"
        context = futu.OpenQuoteContext(host=settings.FUTU_OPEND_HOST, port=settings.FUTU_OPEND_PORT)
        stage = "collect"
        result = collect(context, sys.argv[1], datetime.now(NY))
        stage = "serialize"
        output = json.dumps(result, ensure_ascii=True)
    except CloseDataError as exc:
        error = "calendar_query" if str(exc).startswith("request_trading_days") else "calendar_data"
    except Exception:
        error = stage
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                error = error or "close"
    # Emit after context shutdown so SDK log lines cannot interleave with the frame.
    if error:
        print("\nWHEEL_CLOSE_ERROR:" + error, flush=True)
        sys.exit(1)
    print("\nWHEEL_CLOSE:" + output, flush=True)
