"""Read-only Futu option capability probe with no import-time side effects."""

from __future__ import annotations

import inspect
import math
import os
import re
import sys
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

SUCCESS = "success"
PARTIAL = "partial"
FAILED = "failed"
MAX_SYMBOLS = 20
MAX_DYNAMIC_CANDIDATES = 12
MAX_EXPIRATION_SCAN = 50
MAX_DIVIDEND_CALENDAR_PAGES = 7
DIVIDEND_CALENDAR_PAGE_SIZE = 200
MIN_SUBSCRIPTION_SECONDS = 61.0
MAX_CLEANUP_SLEEP_SLICE = 5.0
SENSITIVE_KEYS = frozenset(
    (
        "host",
        "port",
        "account",
        "牛牛号",
        "niuniu",
        "cookie",
        "token",
        "key",
        "password",
        "secret",
        "api_key",
        "api_secret",
        "account_id",
        "access_token",
        "dsn",
        "database_url",
        "username",
        "user",
    )
)
SENSITIVE_KEY_FRAGMENTS = frozenset(
    (
        "account_id",
        "accountid",
        "access_token",
        "accesstoken",
        "api_key",
        "apikey",
        "api_secret",
        "apisecret",
        "password",
        "secret",
        "cookie",
        "token",
        "database_url",
        "databaseurl",
        "username",
        "db_user",
        "dbuser",
        "database_user",
        "databaseuser",
        "connection_user",
        "connectionuser",
        "dsn",
    )
)
_SYMBOL_RE = re.compile(r"^US\.[A-Z0-9][A-Z0-9.-]*$")
_SENSITIVE_RE = re.compile(
    r"((?:host|port|account|牛牛号|niuniu|cookie|token|key|password|secret|"
    r"api[_-]?key|api[_-]?secret|account[_-]?id|access[_-]?token|dsn|"
    r"database[_-]?url|username|user)"
    r"\s*[:=]\s*)(\S+)",
    re.IGNORECASE,
)
_URL_RE = re.compile(
    r"\b[a-z][a-z0-9+.-]*://[^\s\"'<>]+", re.IGNORECASE
)
_IPV4_RE = re.compile(
    r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?::\d{1,5})?(?![\d.])"
)
_BRACKETED_IPV6_RE = re.compile(
    r"\[[0-9a-f:]+\](?::\d{1,5})?", re.IGNORECASE
)
_IPV6_RE = re.compile(
    r"(?<![0-9a-f:])(?:(?=[0-9a-f:]*::)|"
    r"(?=(?:[0-9a-f]{0,4}:){3}))[0-9a-f:]+(?![0-9a-f:])",
    re.IGNORECASE,
)
_HOST_PORT_RE = re.compile(
    r"\b(?:localhost|[a-z][a-z0-9.-]*):\d{2,5}\b", re.IGNORECASE
)
_BEARER_RE = re.compile(r"\bBearer\s+\S+", re.IGNORECASE)
_LABELED_SECRET_RE = re.compile(
    r"(\b(?:access[_-]?token|api[_-]?key|api[_-]?secret|cookie|password|"
    r"secret|token)\b\s*(?:[:=]\s*)?)"
    r"(?:\"[^\"]*\"|'[^']*'|\S+)",
    re.IGNORECASE,
)
_LABELED_ENDPOINT_RE = re.compile(
    r"(\b(?:account(?:[_-]?id)?|host|port|niuniu|牛牛号|dsn|"
    r"database[_-]?url|username)\b\s*"
    r"(?:[:=]\s*)?)(?:\"[^\"]*\"|'[^']*'|\S+)",
    re.IGNORECASE,
)
_USERINFO_RE = re.compile(
    r"\b[^@\s:/]+:[^@\s]+@[a-z0-9.-]+(?::\d{1,5})?\b",
    re.IGNORECASE,
)

_FIELD_UNITS = {
    "last_price": "provider_price_unknown_currency",
    "security_price": "provider_price_unknown_currency",
    "bid_price": "provider_price_unknown_currency",
    "ask_price": "provider_price_unknown_currency",
    "bid_vol": "provider_volume_unit_unknown",
    "ask_vol": "provider_volume_unit_unknown",
    "volume": "provider_volume_unit_unknown",
    "open_interest": "contracts",
    "implied_volatility": "percent_points",
    "strike_probability": "percent_points",
    "itm_probability": "percent_points",
    "history_volatility": "percent_points",
    "volatility_premium": "percent_points",
    "average_impvol": "percent_points",
    "contract_size": "underlying_shares_per_contract",
    "option_contract_size": "underlying_shares_per_contract",
    "option_owner_lot_multiplier": "underlying_lots_equivalent",
    "owner_lot_multiplier": "underlying_lots_equivalent",
    "option_contract_multiplier": "provider_contract_multiplier",
    "contract_multiplier": "provider_contract_multiplier",
    "delta": "unknown_greek_unit",
    "gamma": "unknown_greek_unit",
    "theta": "unknown_greek_unit",
    "vega": "unknown_greek_unit",
    "rho": "unknown_greek_unit",
    "timestamp": "unix_seconds",
    "timestamp_str": "market_date",
    "update_time": "market_datetime",
    "data_date": "market_date",
    "data_time": "market_time",
    "option_area_type": "enum",
    "impvol_status": "enum",
    "analysis": "text",
    "delay_indicator": "enum",
}

_ANALYTICS_REQUIRED_FIELDS = {
    "get_option_exercise_probability": ("strike_probability",),
    "get_option_volatility": (
        "implied_volatility",
        "history_volatility",
        "volatility_premium",
    ),
}


def validate_symbols(symbols):
    if not symbols:
        raise ValueError("symbols must be a non-empty iterable")
    seen = set()
    result = []
    for symbol in symbols:
        if not isinstance(symbol, str) or not _SYMBOL_RE.fullmatch(symbol):
            raise ValueError(f"invalid symbol: {symbol!r}")
        if symbol not in seen:
            seen.add(symbol)
            result.append(symbol)
    return result


def resolve_profile(
    profile,
    subscribe_quotes,
    include_option_analytics,
    include_history,
    include_earnings,
    allow_partial,
):
    if profile == "static":
        return {
            "profile": "static",
            "subscribe_quotes": bool(subscribe_quotes),
            "include_option_analytics": bool(include_option_analytics),
            "include_history": bool(include_history),
            "include_earnings": bool(include_earnings),
            "allow_partial": bool(allow_partial),
        }
    if profile == "m1-gate":
        if allow_partial:
            raise ValueError("m1-gate does not accept allow_partial")
        return {
            "profile": "m1-gate",
            "subscribe_quotes": True,
            "include_option_analytics": True,
            "include_history": True,
            "include_earnings": True,
            "allow_partial": False,
        }
    raise ValueError(f"unknown profile: {profile!r}")


def field_value(value, raw_field, unit, source_method, as_of, status):
    return {
        "value": value,
        "raw_field": raw_field,
        "unit": unit,
        "source_method": source_method,
        "as_of": as_of,
        "status": status,
    }


def records_from(data):
    if data is None:
        return []
    if hasattr(data, "to_dict"):
        return data.to_dict(orient="records")
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return [data]
    raise ValueError(f"unsupported data type: {type(data).__name__}")


def dividend_records_from(data):
    if not isinstance(data, tuple) or len(data) != 2:
        raise ValueError("unsupported dividend calendar response")
    all_count, records = data
    numeric_count = _non_negative_decimal(all_count)
    if (
        numeric_count is None
        or numeric_count != numeric_count.to_integral_value()
    ):
        raise ValueError("invalid dividend calendar count")
    rows = records_from(records)
    return int(numeric_count), rows


def _is_sensitive_key(key):
    normalized = str(key).strip().lower()
    if normalized in SENSITIVE_KEYS:
        return True
    collapsed = re.sub(r"[^a-z0-9]+", "_", normalized).strip("_")
    compact = collapsed.replace("_", "")
    return any(
        fragment in collapsed or fragment in compact
        for fragment in SENSITIVE_KEY_FRAGMENTS
    )


def _unit_for_field(raw_field):
    return _FIELD_UNITS.get(raw_field, "unknown")


def sanitize_for_output(obj):
    if isinstance(obj, dict):
        return {
            str(key): "***"
            if _is_sensitive_key(key)
            else sanitize_for_output(value)
            for key, value in obj.items()
        }
    if isinstance(obj, (list, tuple)):
        return [sanitize_for_output(value) for value in obj]
    if isinstance(obj, str):
        text = _BEARER_RE.sub("Bearer ***", obj)
        text = _URL_RE.sub("[url]", text)
        text = _USERINFO_RE.sub("[userinfo]", text)
        text = _LABELED_SECRET_RE.sub(r"\1***", text)
        text = _LABELED_ENDPOINT_RE.sub(r"\1***", text)
        text = _SENSITIVE_RE.sub(r"\1***", text)
        text = _BRACKETED_IPV6_RE.sub("[ip]", text)
        text = _IPV4_RE.sub("[ip]", text)
        text = _IPV6_RE.sub("[ip]", text)
        return _HOST_PORT_RE.sub("[endpoint]", text)
    if isinstance(obj, Decimal):
        return str(obj) if obj.is_finite() else None
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    if isinstance(obj, (int, float, bool)) or obj is None:
        return obj
    if hasattr(obj, "item"):
        try:
            return sanitize_for_output(obj.item())
        except (TypeError, ValueError):
            return None
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    return str(obj)


def _strike(record):
    try:
        value = Decimal(str(record.get("strike_price")))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return value if value.is_finite() else None


def _normalize_bool(value):
    if isinstance(value, bool):
        return value
    if hasattr(value, "item"):
        try:
            value = value.item()
        except (TypeError, ValueError):
            return None
        if isinstance(value, bool):
            return value
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        if value == 0:
            return False
        if value == 1:
            return True
        return None
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("false", "no", "0"):
            return False
        if normalized in ("true", "yes", "1"):
            return True
    return None


def _is_standard_contract(record):
    """Accept only an explicitly standard, non-conflicting contract."""
    standard_type = str(record.get("option_standard_type", "")).strip().upper()
    if standard_type not in ("STANDARD", "NORMAL"):
        return False
    if "is_adjusted" in record and _normalize_bool(
        record.get("is_adjusted")
    ) is not False:
        return False
    if "adjustment_status" in record:
        adjustment_status = str(
            record.get("adjustment_status", "")
        ).strip().upper()
        if adjustment_status not in (
            "STANDARD",
            "NORMAL",
            "UNADJUSTED",
            "NON_ADJUSTED",
            "NOT_ADJUSTED",
        ):
            return False
    if "is_non_standard" in record and _normalize_bool(
        record.get("is_non_standard")
    ) is not False:
        return False
    return True


def select_representative_put(records, spot):
    valid = []
    excluded_unknown = 0
    for index, record in enumerate(records or []):
        if str(record.get("option_type", "")).upper() != "PUT":
            continue
        if _is_standard_contract(record) and _strike(record) is not None:
            valid.append((index, record))
        else:
            excluded_unknown += 1

    metadata = {
        "excluded_unknown_adjustment_count": excluded_unknown,
        "degradation": None,
    }
    if not valid:
        metadata["degradation"] = "no standard puts"
        return None, metadata

    earliest_expiration = min(
        str(
            record.get("expiration_date")
            or record.get("strike_time", "")
        )
        for _, record in valid
    )
    group = [
        (index, record)
        for index, record in valid
        if str(
            record.get("expiration_date") or record.get("strike_time", "")
        )
        == earliest_expiration
    ]
    try:
        spot_decimal = Decimal(str(spot)) if spot is not None else None
    except (InvalidOperation, TypeError, ValueError):
        spot_decimal = None

    if spot_decimal is not None and spot_decimal.is_finite() and spot_decimal > 0:
        at_or_below = [
            item for item in group if _strike(item[1]) <= spot_decimal
        ]
        if at_or_below:
            selected = max(
                at_or_below, key=lambda item: _strike(item[1])
            )[1]
        else:
            selected = min(group, key=lambda item: _strike(item[1]))[1]
            metadata["degradation"] = "no strike <= spot; used lowest strike"
    else:
        selected = min(group, key=lambda item: item[0])[1]
        metadata["degradation"] = (
            "spot unavailable; used first standard put in provider order"
        )
    return selected, metadata


def select_representative_call(records, spot):
    valid = [
        (index, record) for index, record in enumerate(records or [])
        if str(record.get("option_type", "")).upper() == "CALL"
        and _is_standard_contract(record) and _strike(record) is not None
    ]
    metadata = {"degradation": None}
    if not valid:
        return None, {"degradation": "no standard calls"}
    try:
        spot_decimal = Decimal(str(spot)) if spot is not None else None
    except (InvalidOperation, TypeError, ValueError):
        spot_decimal = None
    if spot_decimal is not None and spot_decimal.is_finite() and spot_decimal > 0:
        at_or_above = [item for item in valid if _strike(item[1]) >= spot_decimal]
        if at_or_above:
            return min(at_or_above, key=lambda item: _strike(item[1]))[1], metadata
        metadata["degradation"] = "no strike >= spot; used highest strike"
        return max(valid, key=lambda item: _strike(item[1]))[1], metadata
    metadata["degradation"] = "spot unavailable; used first standard call in provider order"
    return valid[0][1], metadata


class ProbeLock:
    """Non-blocking cross-process lock for dynamic subscription operations."""

    def __init__(self, name="futu_option_probe.lock"):
        self._path = os.path.join(tempfile.gettempdir(), name)
        self._file = None
        self._backend = None

    def acquire(self):
        try:
            self._file = open(self._path, "a+b")
            if sys.platform == "win32":
                import msvcrt

                self._file.seek(0)
                if self._file.read(1) == b"":
                    self._file.write(b"\x00")
                    self._file.flush()
                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
                self._backend = msvcrt
            elif os.name == "posix":
                import fcntl

                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._backend = fcntl
            else:
                self._close()
                return False
            return True
        except (ImportError, OSError):
            self._close()
            return False

    def release(self):
        if self._file is None:
            return
        try:
            if sys.platform == "win32" and self._backend is not None:
                self._file.seek(0)
                self._backend.locking(
                    self._file.fileno(), self._backend.LK_UNLCK, 1
                )
            elif os.name == "posix" and self._backend is not None:
                self._backend.flock(self._file.fileno(), self._backend.LOCK_UN)
        except OSError:
            pass
        finally:
            self._close()

    def _close(self):
        if self._file is not None:
            try:
                self._file.close()
            except OSError:
                pass
        self._file = None
        self._backend = None


def _summarize_sdk_error(data, extra=()):
    parts = []
    for value in (data, *extra):
        if value is None:
            continue
        text = str(sanitize_for_output(str(value))).strip()
        if text:
            parts.append(text[:500])
    return " | ".join(parts) if parts else "unknown_error"


def sdk_call(context, method, ret_ok, *args, **kwargs):
    try:
        function = getattr(context, method, None)
    except Exception as exc:
        return {
            "status": "error",
            "category": "method_lookup_error",
            "method": method,
            "data": None,
            "ret_code": None,
            "error": sanitize_for_output(str(exc)),
        }
    if function is None:
        return {
            "status": "unsupported",
            "category": "method_unsupported",
            "method": method,
            "data": None,
            "ret_code": None,
        }
    try:
        response = function(*args, **kwargs)
    except TypeError as exc:
        return {
            "status": "signature_mismatch",
            "category": "signature_mismatch",
            "method": method,
            "data": None,
            "ret_code": None,
            "error": sanitize_for_output(str(exc)),
        }
    except Exception as exc:
        return {
            "status": "error",
            "category": "sdk_exception",
            "method": method,
            "data": None,
            "ret_code": None,
            "error": sanitize_for_output(str(exc)),
        }
    if not isinstance(response, tuple) or len(response) < 2:
        return {
            "status": "invalid_response",
            "category": "invalid_response",
            "method": method,
            "data": None,
            "ret_code": None,
        }
    ret, data, *extra = response
    if ret == ret_ok:
        return {
            "status": "ok",
            "category": None,
            "method": method,
            "data": data,
            "ret_code": sanitize_for_output(ret),
            "extra": extra,
        }
    return {
        "status": "error",
        "category": "provider_error",
        "method": method,
        "data": None,
        "ret_code": sanitize_for_output(ret),
        "error": _summarize_sdk_error(data, extra),
    }


def method_capabilities(context, methods):
    result = {}
    for method in methods:
        try:
            function = getattr(context, method, None)
        except Exception:
            result[method] = {"status": "unknown", "signature": None}
            continue
        if not callable(function):
            result[method] = {
                "status": "unsupported",
                "signature": None,
            }
            continue
        try:
            signature = sanitize_for_output(str(inspect.signature(function)))
        except Exception:
            signature = None
        result[method] = {
            "status": "supported",
            "signature": signature,
        }
    return result


def capability_status(capabilities, method):
    capability = capabilities.get(method)
    if isinstance(capability, dict):
        return capability.get("status", "unknown")
    return "unknown"


def verify_subscription_restored(before, after, owned_codes):
    checks = {
        "before_data_status": before.get("data_status"),
        "after_data_status": after.get("data_status"),
        "quote_codes_match": None,
        "cleanup_candidates_absent": None,
        **{
            f"{field}_match": None
            for field in _SUBSCRIPTION_QUOTA_FIELDS
        },
    }
    if before.get("data_status") != "ok" or after.get("data_status") != "ok":
        return {"status": "partial", "checks": checks}

    before_codes = set(before.get("existing_quote_codes", []))
    after_codes = set(after.get("existing_quote_codes", []))
    candidates = set(owned_codes)
    checks["quote_codes_match"] = before_codes == after_codes
    checks["cleanup_candidates_absent"] = not bool(candidates & after_codes)
    for field in _SUBSCRIPTION_QUOTA_FIELDS:
        try:
            matches = Decimal(str(before.get(field))) == Decimal(
                str(after.get(field))
            )
        except (InvalidOperation, TypeError, ValueError):
            matches = False
        checks[f"{field}_match"] = matches

    status = "restored" if all(checks.values()) else "failed"
    return {"status": status, "checks": checks}


_SUBSCRIPTION_QUOTA_FIELDS = (
    "total_used",
    "remain",
    "own_used",
    "option_used_quota",
    "option_remain_quota",
    "own_option_used_quota",
)


def _is_finite_numeric(value):
    if isinstance(value, bool) or value is None:
        return False
    if hasattr(value, "item"):
        try:
            value = value.item()
        except (TypeError, ValueError):
            return False
    try:
        return Decimal(str(value)).is_finite()
    except (InvalidOperation, TypeError, ValueError):
        return False


def _non_negative_decimal(value):
    if not _is_finite_numeric(value):
        return None
    try:
        numeric = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return numeric if numeric >= 0 else None


def _valid_quote_value(value):
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return all(
            isinstance(item, str) and bool(item.strip()) for item in value
        )
    return False


def _quote_codes(value):
    return [value] if isinstance(value, str) else list(value)


def subscription_summary(data):
    result = {
        **{key: None for key in _SUBSCRIPTION_QUOTA_FIELDS},
        "existing_quote_codes": [],
        "quota_status": "unknown",
        "subscription_list_status": "unknown",
        "data_status": "unknown",
    }
    try:
        rows = records_from(data) if data is not None else []
        if not rows or not isinstance(rows[0], dict):
            return sanitize_for_output(result)
        row = rows[0]
        quota_complete = True
        numeric_quotas = {}
        for key in _SUBSCRIPTION_QUOTA_FIELDS:
            value = row.get(key)
            numeric_value = _non_negative_decimal(value)
            if numeric_value is not None:
                result[key] = sanitize_for_output(value)
                numeric_quotas[key] = numeric_value
            else:
                quota_complete = False
        if quota_complete:
            quota_complete = (
                numeric_quotas["own_used"]
                <= numeric_quotas["total_used"]
                and numeric_quotas["own_option_used_quota"]
                <= numeric_quotas["option_used_quota"]
            )
        result["quota_status"] = "ok" if quota_complete else "unknown"

        quote_codes = []
        sub_list = row.get("sub_list")
        if isinstance(sub_list, dict):
            if "QUOTE" not in sub_list:
                result["subscription_list_status"] = "ok"
            elif _valid_quote_value(sub_list["QUOTE"]):
                quote_codes.extend(_quote_codes(sub_list["QUOTE"]))
                result["subscription_list_status"] = "ok"
        elif isinstance(sub_list, list):
            valid_items = True
            for item in sub_list:
                if (
                    not isinstance(item, dict)
                    or "QUOTE" not in item
                    or not _valid_quote_value(item["QUOTE"])
                ):
                    valid_items = False
                    break
            if valid_items:
                for item in sub_list:
                    quote_codes.extend(_quote_codes(item["QUOTE"]))
                result["subscription_list_status"] = "ok"
        result["existing_quote_codes"] = sorted(set(quote_codes))
        if (
            result["quota_status"] == "ok"
            and result["subscription_list_status"] == "ok"
        ):
            result["data_status"] = "ok"
    except Exception:
        return sanitize_for_output(result)
    return sanitize_for_output(result)


def unsubscribe_owned(context, ret_ok, sub_type, owned_codes):
    if not owned_codes:
        return "not_requested"
    function = getattr(context, "unsubscribe", None)
    quote_type = getattr(sub_type, "QUOTE", None)
    if function is None or quote_type is None:
        return "failed"
    try:
        response = function(
            list(owned_codes), [quote_type], unsubscribe_all=False
        )
    except Exception:
        return "failed"
    if not isinstance(response, tuple) or len(response) < 2:
        return "failed"
    return "restored" if response[0] == ret_ok else "failed"


def _metadata_field(record, raw_field, source_method, as_of, unit=None):
    unit = unit or _unit_for_field(raw_field)
    value = sanitize_for_output(record.get(raw_field))
    return field_value(
        value,
        raw_field,
        unit,
        source_method,
        as_of,
        "ok" if raw_field in record and value is not None else "missing",
    )


def _parse_provider_datetime(value):
    if not isinstance(value, str) or not value.strip():
        return None
    cleaned = value.strip()
    iso_value = cleaned[:-1] + "+00:00" if cleaned.endswith("Z") else cleaned
    try:
        datetime.fromisoformat(iso_value)
    except ValueError:
        try:
            date.fromisoformat(cleaned)
        except ValueError:
            return None
    return cleaned


def _valid_unix_timestamp(value):
    if isinstance(value, bool) or isinstance(value, str) or value is None:
        return None
    if hasattr(value, "item"):
        try:
            value = value.item()
        except (TypeError, ValueError):
            return None
    if not isinstance(value, (int, float, Decimal)):
        return None
    try:
        numeric = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not numeric.is_finite() or numeric <= 0:
        return None
    try:
        parsed = datetime.fromtimestamp(float(numeric), timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    if not 2000 <= parsed.year <= 3000:
        return None
    return sanitize_for_output(value)


def _analytics_time(row):
    for field in ("timestamp_str", "update_time", "time_key"):
        parsed = _parse_provider_datetime(row.get(field))
        if parsed is not None:
            return parsed

    timestamp = _valid_unix_timestamp(row.get("timestamp"))
    if timestamp is not None:
        return timestamp

    data_date = row.get("data_date")
    data_time = row.get("data_time")
    if isinstance(data_date, str) and isinstance(data_time, str):
        parsed = _parse_provider_datetime(
            f"{data_date.strip()} {data_time.strip()}"
        )
        if parsed is not None:
            return parsed
    return None


def _analytics_metadata(row, method, analytics_time):
    fields = {}
    numeric_fields = set(_ANALYTICS_REQUIRED_FIELDS.get(method, ()))
    for raw_field, raw_value in row.items():
        value = sanitize_for_output(raw_value)
        if raw_field == "timestamp":
            field_status = (
                "ok"
                if _valid_unix_timestamp(raw_value) is not None
                else "missing"
            )
        elif raw_field in ("timestamp_str", "update_time", "time_key"):
            field_status = (
                "ok"
                if _parse_provider_datetime(raw_value) is not None
                else "missing"
            )
        elif raw_field in numeric_fields:
            field_status = (
                "ok" if _is_finite_numeric(raw_value) else "missing"
            )
        else:
            field_status = "ok" if value is not None else "missing"
        if field_status != "ok":
            value = None
        fields[raw_field] = field_value(
            value,
            raw_field,
            _unit_for_field(raw_field),
            method,
            analytics_time,
            field_status,
        )

    required_fields = list(_ANALYTICS_REQUIRED_FIELDS.get(method, ()))
    missing_required = [
        raw_field
        for raw_field in required_fields
        if fields.get(raw_field, {}).get("status") != "ok"
    ]
    has_time = bool(analytics_time)
    return {
        "status": "ok" if not missing_required and has_time else "partial",
        "source_method": method,
        "as_of_status": "ok" if has_time else "unknown",
        "required_fields": required_fields,
        "missing_required_fields": missing_required,
        "fields": fields,
    }


def _sdk_issue(source, response, status=None):
    return sanitize_for_output(
        {
            "source": source,
            "status": status or response.get("status", "unknown"),
            "category": response.get("category", "unknown"),
            "ret_code": response.get("ret_code"),
            "error": response.get("error"),
        }
    )


def _fetch_dividend_calendar(context, ret_ok, market, query_date):
    rows = []
    expected_count = None
    last_response = None
    for _ in range(MAX_DIVIDEND_CALENDAR_PAGES):
        response = sdk_call(
            context,
            "get_dividend_calendar",
            ret_ok,
            market,
            str(query_date),
            data_from=len(rows),
            count=DIVIDEND_CALENDAR_PAGE_SIZE,
        )
        last_response = response
        if response["status"] != "ok":
            return response, []
        try:
            page_count, page_rows = dividend_records_from(response["data"])
        except (TypeError, ValueError):
            return {
                **response,
                "status": "partial",
                "category": "malformed_data",
            }, []
        if expected_count is None:
            expected_count = page_count
        if page_count != expected_count:
            return {
                **response,
                "status": "partial",
                "category": "inconsistent_page_count",
            }, []
        rows.extend(page_rows)
        if len(rows) == expected_count:
            return response, rows
        if len(rows) > expected_count or not page_rows:
            return {
                **response,
                "status": "partial",
                "category": "incomplete_pagination",
            }, []
    return {
        **(last_response or {}),
        "status": "partial",
        "category": "pagination_limit",
    }, []


def _expiration_detail(raw_value, probe_dt):
    try:
        cleaned = str(raw_value).strip()
        if len(cleaned) >= 8 and cleaned[:8].isdigit():
            expiration_date = date(
                int(cleaned[:4]), int(cleaned[4:6]), int(cleaned[6:8])
            )
        else:
            expiration_date = date.fromisoformat(cleaned[:10])
        if probe_dt.tzinfo is not None:
            probe_dt = probe_dt.astimezone(ZoneInfo("America/New_York"))
        dte = (expiration_date - probe_dt.date()).days
        return {
            "strike_time": raw_value,
            "date": expiration_date.isoformat(),
            "weekday": expiration_date.strftime("%A"),
            "dte": dte,
            "is_7_to_30_dte": 7 <= dte <= 30,
            "status": "ok",
        }
    except (TypeError, ValueError, OverflowError):
        return {
            "strike_time": raw_value,
            "date": None,
            "weekday": None,
            "dte": None,
            "is_7_to_30_dte": None,
            "status": "parse_error",
        }


def _quote_time_quality(raw_value, probe_dt, max_age_seconds=600):
    """Classify US quote timestamps documented by Futu as New York time."""
    if not raw_value:
        return "unknown", "unknown"
    try:
        parsed = datetime.fromisoformat(str(raw_value).strip())
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo("America/New_York"))
        reference = probe_dt
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=ZoneInfo("America/New_York"))
        else:
            reference = reference.astimezone(ZoneInfo("America/New_York"))
        age = (reference - parsed).total_seconds()
    except (TypeError, ValueError, OverflowError):
        return "unknown", "unknown"
    if -5 <= age <= max_age_seconds:
        return "real_time", "fresh"
    if age > max_age_seconds:
        return "delayed", "stale"
    return "unknown", "unknown"
def probe_symbol(
    context,
    futu_module,
    symbol,
    config,
    max_expirations,
    max_contracts_per_expiration,
    existing_quote_codes,
    owned_codes,
    probe_dt=None,
    market_state=None,
    event_calendar_cache=None,
    subscription_started_at=None,
    monotonic=None,
    include_covered_call=True,
):
    """Probe one underlying while isolating provider failures from other symbols."""
    ret_ok = getattr(futu_module, "RET_OK", 0)
    errors = []
    partial = False
    underlying_quote = {}
    spot = None
    probe_dt = probe_dt or datetime.now(ZoneInfo("America/New_York"))
    event_calendar_cache = (
        event_calendar_cache
        if event_calendar_cache is not None
        else {}
    )
    subscription_started_at = (
        subscription_started_at
        if subscription_started_at is not None
        else {}
    )
    monotonic = monotonic or time.monotonic
    market_state = market_state or {
        "status": "not_requested",
        "market_us": None,
    }
    if (
        config["profile"] == "m1-gate"
        and market_state.get("market_us") not in {"MORNING", "AFTERNOON"}
    ):
        partial = True
        errors.append("market_session:not_regular")

    snapshot = sdk_call(context, "get_market_snapshot", ret_ok, [symbol])
    if snapshot["status"] == "ok":
        rows = records_from(snapshot["data"])
        if rows:
            row = rows[0]
            underlying_quote = {
                "last_price": row.get("last_price"),
                "update_time": row.get("update_time"),
                "sec_status": row.get("sec_status"),
                "suspension": row.get("suspension"),
            }
            spot = row.get("last_price")
        else:
            partial = True
            errors.append("underlying_snapshot:empty")
    else:
        partial = True
        errors.append(_sdk_issue("underlying_snapshot", snapshot))

    expirations = []
    expiration_details = []
    rejected_expirations = []
    expiration_response = sdk_call(
        context, "get_option_expiration_date", ret_ok, symbol
    )
    if expiration_response["status"] == "ok":
        expiration_rows = records_from(expiration_response["data"])
        scanned_expirations = sorted(
            {
                str(row["strike_time"])
                for row in expiration_rows
                if row.get("strike_time")
            }
        )
        if len(scanned_expirations) > MAX_EXPIRATION_SCAN:
            scanned_expirations = scanned_expirations[:MAX_EXPIRATION_SCAN]
            partial = True
            errors.append("expiration_date:truncated_to_max_scan")

        eligible_details = []
        for expiration in scanned_expirations:
            detail = _expiration_detail(expiration, probe_dt)
            if detail["status"] != "ok":
                rejected_expirations.append(detail)
                partial = True
                errors.append(
                    {
                        "source": "expiration_date",
                        "status": "parse_error",
                        "category": "field_missing",
                        "strike_time": expiration,
                    }
                )
            elif detail["dte"] < 0:
                detail["status"] = "expired"
                rejected_expirations.append(detail)
            else:
                eligible_details.append(detail)

        if config.get("profile") == "m1-gate":
            preferred = [item for item in eligible_details if item["is_7_to_30_dte"]]
            other_future = [item for item in eligible_details if item["dte"] > 0 and not item["is_7_to_30_dte"]]
            eligible_details = preferred + other_future
        expiration_details = eligible_details[:max_expirations]
        expirations = [
            detail["strike_time"] for detail in expiration_details
        ]
        if not expirations:
            partial = True
            errors.append("expiration_date:no_eligible_expiration")
    else:
        partial = True
        errors.append(_sdk_issue("expiration_date", expiration_response))

    chain_summary = {}
    representatives = []
    option_type = getattr(futu_module, "OptionType", None)
    all_options = getattr(option_type, "ALL", "ALL")
    for expiration in expirations:
        chain_response = sdk_call(
            context,
            "get_option_chain",
            ret_ok,
            symbol,
            start=expiration,
            end=expiration,
            option_type=all_options,
        )
        if chain_response["status"] != "ok":
            partial = True
            errors.append(_sdk_issue(f"chain_{expiration}", chain_response))
            continue
        chain_rows = records_from(chain_response["data"])
        put_rows = [
            row
            for row in chain_rows
            if str(row.get("option_type", "")).upper() == "PUT"
        ]
        call_rows = [
            row for row in chain_rows
            if str(row.get("option_type", "")).upper() == "CALL"
        ]
        call_count = sum(
            str(row.get("option_type", "")).upper() == "CALL"
            for row in chain_rows
        )
        standard_count = sum(
            str(row.get("option_standard_type", "")).upper()
            in ("STANDARD", "NORMAL")
            for row in chain_rows
        )
        nonstandard_count = sum(
            str(row.get("option_standard_type", "")).upper()
            == "NON_STANDARD"
            for row in chain_rows
        )
        unknown_count = len(chain_rows) - standard_count - nonstandard_count
        chain_summary[expiration] = {
            "total": len(chain_rows),
            "puts": len(put_rows),
            "calls": call_count,
            "standard": standard_count,
            "non_standard": nonstandard_count,
            "unknown_standard_status": unknown_count,
        }
        if unknown_count:
            partial = True

        remaining = list(put_rows)
        include_call = (
            config.get("profile") == "m1-gate"
            and max_contracts_per_expiration > 1
            and include_covered_call
        )
        put_limit = max_contracts_per_expiration - 1 if include_call else max_contracts_per_expiration
        for _ in range(put_limit):
            selected, metadata = select_representative_put(remaining, spot)
            if selected is None:
                break
            settlement_mode = selected.get("option_settlement_mode")
            identity_unknown_fields = [
                "deliverable_shares",
                "exercise_style",
            ]
            if str(settlement_mode).strip().upper() != "PHYSICAL":
                identity_unknown_fields.append("option_settlement_mode")
            representatives.append(
                {
                    "code": selected.get("code"),
                    "option_type": selected.get("option_type"),
                    "stock_owner": selected.get("stock_owner"),
                    "strike_time": selected.get("strike_time"),
                    "expiration_date": selected.get("expiration_date"),
                    "strike_price": selected.get("strike_price"),
                    "option_standard_type": selected.get(
                        "option_standard_type"
                    ),
                    "lot_size": selected.get("lot_size"),
                    "option_settlement_mode": settlement_mode,
                    "settlement_evidence": "unknown",
                    "index_option_type": selected.get("index_option_type"),
                    "deliverable_shares": None,
                    "exercise_style": None,
                    "contract_identity_status": "partial",
                    "identity_unknown_fields": identity_unknown_fields,
                    "degradation": metadata.get("degradation"),
                }
            )
            remaining.remove(selected)
        if include_call:
            selected, metadata = select_representative_call(call_rows, spot)
            if selected is not None:
                settlement_mode = selected.get("option_settlement_mode")
                identity_unknown_fields = ["deliverable_shares", "exercise_style"]
                if str(settlement_mode).strip().upper() != "PHYSICAL":
                    identity_unknown_fields.append("option_settlement_mode")
                representatives.append(
                    {
                        "code": selected.get("code"), "option_type": selected.get("option_type"),
                        "stock_owner": selected.get("stock_owner"), "strike_time": selected.get("strike_time"),
                        "expiration_date": selected.get("expiration_date"), "strike_price": selected.get("strike_price"),
                        "option_standard_type": selected.get("option_standard_type"), "lot_size": selected.get("lot_size"),
                        "option_settlement_mode": settlement_mode, "settlement_evidence": "unknown",
                        "index_option_type": selected.get("index_option_type"), "deliverable_shares": None,
                        "exercise_style": None, "contract_identity_status": "partial",
                        "identity_unknown_fields": identity_unknown_fields, "degradation": metadata.get("degradation"),
                    }
                )
    if expirations and not representatives:
        partial = True
        errors.append("representative_contract:no_standard_put")

    if config["subscribe_quotes"]:
        known_subscribed = set(existing_quote_codes) | set(owned_codes)
        sub_type = getattr(futu_module, "SubType", None)
        quote_subtype = getattr(sub_type, "QUOTE", None)
        if quote_subtype is None:
            partial = True
            errors.append("subscription:quote_subtype_unsupported")
        else:
            for contract in representatives:
                code = contract.get("code")
                if not code:
                    partial = True
                    continue
                if code not in known_subscribed:
                    known_subscribed.add(code)
                    owned_codes.append(code)
                    try:
                        subscribe_response = sdk_call(
                            context,
                            "subscribe",
                            ret_ok,
                            [code],
                            [quote_subtype],
                            subscribe_push=False,
                        )
                    finally:
                        subscription_started_at[code] = monotonic()
                    if subscribe_response["status"] != "ok":
                        partial = True
                        errors.append(
                            _sdk_issue(
                                f"subscribe_{code}", subscribe_response
                            )
                        )
                        continue

                option_snapshot = sdk_call(
                    context, "get_market_snapshot", ret_ok, [code]
                )
                snapshot_rows = (
                    records_from(option_snapshot["data"])
                    if option_snapshot["status"] == "ok"
                    else []
                )
                snapshot_row = snapshot_rows[0] if snapshot_rows else {}
                snapshot_time = snapshot_row.get("update_time")
                if option_snapshot["status"] != "ok":
                    errors.append(
                        _sdk_issue(f"option_snapshot_{code}", option_snapshot)
                    )
                elif not snapshot_rows:
                    errors.append(
                        {
                            "source": f"option_snapshot_{code}",
                            "status": "empty",
                            "category": "empty",
                        }
                    )
                dynamic = {
                    field: _metadata_field(
                        snapshot_row, field, "get_market_snapshot", snapshot_time
                    )
                    for field in (
                        "bid_price",
                        "ask_price",
                        "bid_vol",
                        "ask_vol",
                        "option_contract_size",
                        "option_owner_lot_multiplier",
                        "option_contract_multiplier",
                        "option_area_type",
                    )
                }
                snapshot_delay, snapshot_freshness = _quote_time_quality(snapshot_time, probe_dt)
                dynamic["snapshot_delay_status"] = field_value(
                    snapshot_delay,
                    "delay_indicator",
                    "categorical",
                    "get_market_snapshot",
                    snapshot_time,
                    "ok" if snapshot_delay != "unknown" else "unknown",
                )
                dynamic["snapshot_freshness_status"] = field_value(
                    snapshot_freshness,
                    "update_time",
                    "categorical",
                    "get_market_snapshot",
                    snapshot_time,
                    "ok" if snapshot_freshness != "unknown" else "unknown",
                )

                quote_response = sdk_call(
                    context, "get_stock_quote", ret_ok, [code]
                )
                quote_rows = (
                    records_from(quote_response["data"])
                    if quote_response["status"] == "ok"
                    else []
                )
                quote_row = quote_rows[0] if quote_rows else {}
                if quote_response["status"] != "ok":
                    errors.append(_sdk_issue(f"option_quote_{code}", quote_response))
                elif not quote_rows:
                    errors.append(
                        {
                            "source": f"option_quote_{code}",
                            "status": "empty",
                            "category": "empty",
                        }
                    )
                quote_time = " ".join(
                    filter(
                        None,
                        (
                            str(quote_row.get("data_date", "")),
                            str(quote_row.get("data_time", "")),
                        ),
                    )
                ) or None
                for field in (
                    "last_price",
                    "volume",
                    "open_interest",
                    "implied_volatility",
                    "delta",
                    "gamma",
                    "theta",
                    "vega",
                    "rho",
                    "contract_size",
                ):
                    dynamic[field] = _metadata_field(
                        quote_row, field, "get_stock_quote", quote_time
                    )

                snapshot_contract_size = _non_negative_decimal(
                    snapshot_row.get("option_contract_size")
                )
                quote_contract_size = _non_negative_decimal(
                    quote_row.get("contract_size")
                )
                static_lot_size = _non_negative_decimal(
                    contract.get("lot_size")
                )
                option_area_type = str(
                    snapshot_row.get("option_area_type", "")
                ).upper()
                valid_exercise_styles = {
                    "AMERICAN",
                    "EUROPEAN",
                    "BERMUDA",
                }
                size_values = (
                    snapshot_contract_size,
                    quote_contract_size,
                    static_lot_size,
                )
                if (
                    all(
                        value is not None and value > 0
                        for value in size_values
                    )
                    and len(set(size_values)) == 1
                    and all(
                        value == value.to_integral_value()
                        for value in size_values
                    )
                ):
                    contract["deliverable_shares"] = sanitize_for_output(
                        snapshot_row.get("option_contract_size")
                    )
                if option_area_type in valid_exercise_styles:
                    contract["exercise_style"] = option_area_type
                identity_unknown_fields = [
                    field
                    for field in ("deliverable_shares", "exercise_style")
                    if contract.get(field) is None
                ]
                if (
                    str(contract.get("option_settlement_mode"))
                    .strip()
                    .upper()
                    != "PHYSICAL"
                ):
                    standard_equity_fallback = (
                        str(contract.get("option_standard_type", "")).upper()
                        in {"STANDARD", "NORMAL"}
                        and str(contract.get("index_option_type", "")).upper() == "N/A"
                        and contract.get("deliverable_shares") == 100
                        and contract.get("exercise_style") == "AMERICAN"
                    )
                    if standard_equity_fallback:
                        contract["settlement_evidence"] = "occ_standard_equity"
                    else:
                        identity_unknown_fields.append("option_settlement_mode")
                else:
                    contract["settlement_evidence"] = "provider_physical"
                contract["identity_unknown_fields"] = identity_unknown_fields
                contract["contract_identity_status"] = (
                    "ok" if not identity_unknown_fields else "partial"
                )
                quote_delay, quote_freshness = _quote_time_quality(quote_time, probe_dt)
                dynamic["quote_delay_status"] = field_value(
                    quote_delay,
                    "delay_indicator",
                    "categorical",
                    "get_stock_quote",
                    quote_time,
                    "ok" if quote_delay != "unknown" else "unknown",
                )
                dynamic["quote_freshness_status"] = field_value(
                    quote_freshness,
                    "data_date+data_time",
                    "categorical",
                    "get_stock_quote",
                    quote_time,
                    "ok" if quote_freshness != "unknown" else "unknown",
                )
                critical_fields = (
                    "bid_price",
                    "ask_price",
                    "bid_vol",
                    "ask_vol",
                    "last_price",
                    "volume",
                    "open_interest",
                    "implied_volatility",
                    "delta",
                    "gamma",
                    "theta",
                    "vega",
                    "rho",
                )
                if any(
                    dynamic[field]["status"] != "ok"
                    for field in critical_fields
                ):
                    partial = True
                if contract["contract_identity_status"] != "ok":
                    partial = True
                contract["dynamic_quote"] = dynamic

    if any(
        contract.get("contract_identity_status") != "ok"
        for contract in representatives
    ):
        partial = True

    if config["include_option_analytics"]:
        for contract in representatives:
            code = contract.get("code")
            analytics = {}
            for label, method in (
                ("probability", "get_option_exercise_probability"),
                ("volatility", "get_option_volatility"),
            ):
                response = sdk_call(context, method, ret_ok, code)
                if response["status"] != "ok":
                    analytics[label] = {
                        "status": response["status"],
                        "source_method": method,
                        "fields": {},
                        "issue": _sdk_issue(method, response),
                    }
                    partial = True
                    continue
                rows = records_from(response["data"])
                if not rows or not isinstance(rows[0], dict):
                    analytics[label] = {
                        "status": "empty",
                        "source_method": method,
                        "fields": {},
                        "issue": {
                            "source": method,
                            "status": "empty",
                            "category": "empty",
                        },
                    }
                    partial = True
                    continue
                row = rows[0]
                analytics_time = _analytics_time(row)
                analytics[label] = _analytics_metadata(
                    row, method, analytics_time
                )
                if analytics[label]["status"] != "ok":
                    partial = True
            contract["analytics"] = analytics

    history = {"status": "not_requested"}
    if config["include_history"]:
        k_day = getattr(getattr(futu_module, "KLType", None), "K_DAY", None)
        qfq = getattr(getattr(futu_module, "AuType", None), "QFQ", None)
        if k_day is None or qfq is None:
            history = {"status": "unsupported", "sample_count": 0}
            partial = True
        else:
            response = sdk_call(
                context,
                "request_history_kline",
                ret_ok,
                symbol,
                ktype=k_day,
                autype=qfq,
                max_count=250,
            )
            rows = (
                records_from(response["data"])
                if response["status"] == "ok"
                else []
            )
            history = {
                "status": "ok"
                if rows
                else (
                    "empty" if response["status"] == "ok" else response["status"]
                ),
                "sample_count": len(rows),
                "last_date": (
                    rows[-1].get("time_key") or rows[-1].get("date")
                    if rows
                    else None
                ),
                "records": sanitize_for_output(rows),
            }
            if response["status"] != "ok":
                history["issue"] = _sdk_issue(
                    "request_history_kline", response
                )
            if not rows:
                partial = True

    earnings = {"status": "not_requested", "records": []}
    ex_dividend = {"status": "not_requested"}
    if config["include_earnings"]:
        market_us = getattr(getattr(futu_module, "Market", None), "US", None)
        if market_us is None:
            earnings = {"status": "unsupported", "records": []}
        else:
            if probe_dt.tzinfo is not None:
                event_date = probe_dt.astimezone(
                    ZoneInfo("America/New_York")
                ).date()
            else:
                event_date = probe_dt.date()
            event_end_date = event_date + timedelta(days=6)
            earnings_cache_key = (
                "earnings",
                str(event_date),
                str(event_end_date),
            )
            if earnings_cache_key not in event_calendar_cache:
                response = sdk_call(
                    context,
                    "get_earnings_calendar",
                    ret_ok,
                    market_us,
                    begin_date=str(event_date),
                    end_date=str(event_end_date),
                )
                rows = (
                    records_from(response["data"])
                    if response["status"] == "ok"
                    else []
                )
                event_calendar_cache[earnings_cache_key] = response, rows
            else:
                response, rows = event_calendar_cache[earnings_cache_key]
            matching = [
                row
                for row in rows
                if str(row.get("security", "")) == symbol
                or str(row.get("code", "")) == symbol
            ]
            earnings = {
                "status": (
                    "ok"
                    if response["status"] == "ok"
                    else response["status"]
                ),
                "event_status": "blocked" if matching else "clear",
                "query_window": {
                    "begin": str(event_date),
                    "end": str(event_end_date),
                },
                "records": sanitize_for_output(matching),
            }
            if response["status"] != "ok":
                earnings["issue"] = _sdk_issue(
                    "get_earnings_calendar", response
                )
        dividend_method = getattr(context, "get_dividend_calendar", None)
        if not callable(dividend_method) or market_us is None:
            ex_dividend = {
                "status": "unsupported",
                "category": "method_unsupported",
                "records": [],
            }
        else:
            dividend_rows = []
            dividend_issues = []
            query_dates = []
            for day_offset in range(7):
                query_date = event_date + timedelta(days=day_offset)
                query_dates.append(str(query_date))
                dividend_cache_key = ("dividend", str(query_date))
                if dividend_cache_key not in event_calendar_cache:
                    response, rows = _fetch_dividend_calendar(
                        context,
                        ret_ok,
                        market_us,
                        query_date,
                    )
                    event_calendar_cache[dividend_cache_key] = response, rows
                else:
                    response, rows = event_calendar_cache[
                        dividend_cache_key
                    ]
                if response["status"] == "ok":
                    dividend_rows.extend(rows)
                else:
                    dividend_issues.append(
                        _sdk_issue("get_dividend_calendar", response)
                    )

            matching_dividends = [
                row
                for row in dividend_rows
                if str(row.get("security", "")) == symbol
                or str(row.get("code", "")) == symbol
            ]
            ex_dividend = {
                "status": (
                    "partial"
                    if dividend_issues
                    else "ok"
                ),
                "event_status": "blocked" if matching_dividends else "clear",
                "query_dates": query_dates,
                "records": sanitize_for_output(matching_dividends),
            }
            if dividend_issues:
                ex_dividend["issues"] = dividend_issues
        if earnings["status"] != "ok" or ex_dividend["status"] != "ok":
            partial = True

    return sanitize_for_output(
        {
            "symbol": symbol,
            "status": PARTIAL if partial else SUCCESS,
            "market_state": market_state,
            "underlying_quote": underlying_quote,
            "expirations": expiration_details,
            "rejected_expirations": rejected_expirations,
            "chain_summary": chain_summary,
            "representative_contracts": representatives,
            "history": history,
            "earnings": earnings,
            "ex_dividend": ex_dividend,
            "errors": errors,
        }
    )


def run_probe(
    symbols,
    max_expirations=1,
    max_contracts_per_expiration=1,
    profile="static",
    subscribe_quotes=False,
    include_option_analytics=False,
    include_history=False,
    include_earnings=False,
    allow_partial=False,
    futu_module=None,
    context_factory=None,
    lock_factory=None,
    monotonic=None,
    sleeper=None,
    covered_call_symbols=None,
):
    """Run one isolated capability probe and return a JSON-safe result."""
    probe_now = datetime.now(timezone.utc)
    fetched_at = probe_now.isoformat()
    monotonic = monotonic or time.monotonic
    sleeper = sleeper or time.sleep

    def failed_result(messages):
        return sanitize_for_output(
            {
                "status": FAILED,
                "sdk_version": None,
                "fetched_at": fetched_at,
                "subscription": {
                    "owned_codes": [],
                    "cleanup_status": "not_requested",
                    "verification": {
                        "status": "not_requested",
                        "checks": {},
                    },
                },
                "capabilities": {},
                "symbols": [],
                "errors": messages,
                "profile": profile,
            }
        )

    try:
        normalized_symbols = validate_symbols(symbols)
        if covered_call_symbols is None:
            normalized_covered_call_symbols = set(normalized_symbols)
        elif covered_call_symbols:
            normalized_covered_call_symbols = set(
                validate_symbols(covered_call_symbols)
            )
        else:
            normalized_covered_call_symbols = set()
        if not normalized_covered_call_symbols.issubset(set(normalized_symbols)):
            raise ValueError("covered call symbols must be a subset of symbols")
        if len(normalized_symbols) > MAX_SYMBOLS:
            raise ValueError(
                f"symbols count exceeds MAX_SYMBOLS={MAX_SYMBOLS}"
            )
        if not 1 <= max_expirations <= 3:
            raise ValueError("max_expirations must be between 1 and 3")
        if not 1 <= max_contracts_per_expiration <= 3:
            raise ValueError(
                "max_contracts_per_expiration must be between 1 and 3"
            )
        config = resolve_profile(
            profile,
            subscribe_quotes,
            include_option_analytics,
            include_history,
            include_earnings,
            allow_partial,
        )
        worst_case_candidates = (
            len(normalized_symbols)
            * max_expirations
            * max_contracts_per_expiration
        )
        if (
            config["subscribe_quotes"]
            and worst_case_candidates > MAX_DYNAMIC_CANDIDATES
        ):
            raise ValueError(
                "dynamic candidate limit exceeded: "
                f"{worst_case_candidates}>{MAX_DYNAMIC_CANDIDATES}"
            )
    except (TypeError, ValueError) as exc:
        return failed_result([sanitize_for_output(str(exc))])

    if futu_module is None:
        try:
            import futu as futu_module
        except Exception:
            return failed_result(["futu_sdk_import_failed"])

    lock = None
    if config["subscribe_quotes"]:
        try:
            lock = (lock_factory or ProbeLock)()
            lock_acquired = lock.acquire()
        except Exception:
            lock_acquired = False
        if not lock_acquired:
            if lock is not None:
                try:
                    lock.release()
                except Exception:
                    pass
            return failed_result(["dynamic_subscription_lock_unavailable"])

    context = None
    try:
        if context_factory is not None:
            context = context_factory()
        else:
            from django.conf import settings

            context = futu_module.OpenQuoteContext(
                host=settings.FUTU_OPEND_HOST,
                port=settings.FUTU_OPEND_PORT,
            )
    except Exception:
        if lock is not None:
            try:
                lock.release()
            except Exception:
                pass
        return failed_result(["quote_context_connection_failed"])

    ret_ok = 0
    sdk_version = None
    methods = (
        "query_subscription",
        "get_market_snapshot",
        "get_global_state",
        "get_option_expiration_date",
        "get_option_chain",
        "subscribe",
        "unsubscribe",
        "get_stock_quote",
        "get_option_exercise_probability",
        "get_option_volatility",
        "request_history_kline",
        "get_earnings_calendar",
        "get_dividend_calendar",
    )
    capabilities = {
        method: {"status": "unknown", "signature": None}
        for method in methods
    }
    owned_codes = []
    subscription_started_at = {}
    errors = []
    symbol_results = []
    market_state = {
        "status": "not_requested",
        "market_us": None,
        "timestamp": None,
    }
    before = subscription_summary(None)
    after = subscription_summary(None)
    cleanup_status = "not_requested"
    cleanup_call_status = "not_requested"
    cleanup_wait_seconds = 0.0
    verification = {"status": "not_requested", "checks": {}}
    status = SUCCESS
    any_partial = False
    all_failed = True
    pending_base_exception = None

    try:
        ret_ok = getattr(futu_module, "RET_OK", 0)
        sdk_version = getattr(futu_module, "__version__", None)
        capabilities = method_capabilities(context, methods)

        if config["profile"] == "m1-gate":
            global_state_response = sdk_call(
                context, "get_global_state", ret_ok
            )
            if global_state_response["status"] == "ok":
                rows = records_from(global_state_response["data"])
                row = rows[0] if rows else {}
                market_state = {
                    "status": "ok" if row.get("market_us") else "partial",
                    "market_us": row.get("market_us"),
                    "timestamp": row.get("timestamp"),
                }
            else:
                market_state = {
                    "status": "failed",
                    "market_us": None,
                    "timestamp": None,
                }
            if market_state["market_us"] not in {"MORNING", "AFTERNOON"}:
                any_partial = True
                errors.append("market_session:not_regular")

        missing_required = []
        if config["subscribe_quotes"]:
            required_methods = (
                "query_subscription",
                "subscribe",
                "unsubscribe",
                "get_market_snapshot",
                "get_stock_quote",
            )
            missing_required = [
                method
                for method in required_methods
                if capability_status(capabilities, method) != "supported"
            ]
            if missing_required:
                status = FAILED
                errors.append(
                    "required_methods_unsupported:"
                    + ",".join(missing_required)
                )

        optional_methods = (
            (
                "get_option_exercise_probability",
                config["include_option_analytics"],
            ),
            ("get_option_volatility", config["include_option_analytics"]),
            ("request_history_kline", config["include_history"]),
            ("get_earnings_calendar", config["include_earnings"]),
            ("get_dividend_calendar", config["include_earnings"]),
        )
        for method, requested in optional_methods:
            if (
                requested
                and capability_status(capabilities, method) != "supported"
            ):
                any_partial = True
                errors.append(f"optional_method_unsupported:{method}")

        before_response = sdk_call(
            context, "query_subscription", ret_ok, False
        )
        if before_response["status"] == "ok":
            before = subscription_summary(before_response["data"])
            if before.get("data_status") != "ok":
                any_partial = True
                errors.append(
                    {
                        "source": "subscription_before",
                        "status": "incomplete",
                        "category": "field_missing",
                    }
                )
        else:
            errors.append(
                _sdk_issue("subscription_before", before_response)
            )
            if config["subscribe_quotes"]:
                status = FAILED
            else:
                any_partial = True

        skip_symbols = bool(missing_required)
        if config["subscribe_quotes"] and (
            before_response["status"] != "ok"
            or before.get("data_status") != "ok"
        ):
            skip_symbols = True
            status = FAILED
            errors.append(
                {
                    "source": "subscription_before",
                    "status": "incomplete",
                    "category": "field_missing",
                    "quota_status": before.get("quota_status"),
                    "subscription_list_status": before.get(
                        "subscription_list_status"
                    ),
                }
            )

        if not skip_symbols:
            existing_codes = set(before.get("existing_quote_codes", []))
            event_calendar_cache = {}
            for symbol in normalized_symbols:
                try:
                    symbol_result = probe_symbol(
                        context,
                        futu_module,
                        symbol,
                        config,
                        max_expirations,
                        max_contracts_per_expiration,
                        existing_codes,
                        owned_codes,
                        probe_dt=probe_now,
                        market_state=market_state,
                        event_calendar_cache=event_calendar_cache,
                        subscription_started_at=subscription_started_at,
                        monotonic=monotonic,
                        include_covered_call=(symbol in normalized_covered_call_symbols),
                    )
                except Exception:
                    symbol_result = {
                        "symbol": symbol,
                        "status": FAILED,
                        "errors": ["unexpected_symbol_probe_error"],
                    }
                symbol_results.append(symbol_result)
                if symbol_result.get("status") != FAILED:
                    all_failed = False
                if symbol_result.get("status") in (PARTIAL, FAILED):
                    any_partial = True
            if all_failed:
                status = FAILED
            elif any_partial and status == SUCCESS:
                status = PARTIAL
    except Exception:
        status = FAILED
        errors.append("unexpected_probe_runtime_error")
    except BaseException as exc:
        pending_base_exception = exc
        status = FAILED
        errors.append("probe_interrupted")
    finally:
        if config["subscribe_quotes"]:
            if owned_codes:
                try:
                    cleanup_deadline = max(
                        subscription_started_at[code]
                        for code in owned_codes
                    ) + MIN_SUBSCRIPTION_SECONDS
                    while True:
                        remaining_seconds = cleanup_deadline - monotonic()
                        if remaining_seconds <= 0:
                            break
                        wait_seconds = min(
                            MAX_CLEANUP_SLEEP_SLICE,
                            remaining_seconds,
                        )
                        try:
                            sleeper(wait_seconds)
                            cleanup_wait_seconds += wait_seconds
                        except Exception:
                            cleanup_call_status = "failed"
                            errors.append(
                                "subscription_cleanup_wait_exception"
                            )
                            break
                        except BaseException as exc:
                            if pending_base_exception is None:
                                pending_base_exception = exc
                            continue
                except Exception:
                    cleanup_call_status = "failed"
                    errors.append("subscription_cleanup_exception")
                except BaseException as exc:
                    cleanup_call_status = "failed"
                    if pending_base_exception is None:
                        pending_base_exception = exc

                try:
                    unsubscribe_status = unsubscribe_owned(
                        context,
                        ret_ok,
                        getattr(futu_module, "SubType", None),
                        owned_codes,
                    )
                    if cleanup_call_status != "failed":
                        cleanup_call_status = unsubscribe_status
                except Exception:
                    cleanup_call_status = "failed"
                    errors.append("subscription_cleanup_exception")
                except BaseException as exc:
                    cleanup_call_status = "failed"
                    if pending_base_exception is None:
                        pending_base_exception = exc

        try:
            after_response = sdk_call(
                context, "query_subscription", ret_ok, False
            )
            if after_response["status"] == "ok":
                after = subscription_summary(after_response["data"])
                if after.get("data_status") != "ok":
                    any_partial = True
                    errors.append(
                        {
                            "source": "subscription_after",
                            "status": "incomplete",
                            "category": "field_missing",
                        }
                    )
            else:
                any_partial = True
                errors.append(
                    _sdk_issue("subscription_after", after_response)
                )
        except Exception:
            any_partial = True
            errors.append("subscription_after_query_exception")
        except BaseException as exc:
            any_partial = True
            if pending_base_exception is None:
                pending_base_exception = exc

        if config["subscribe_quotes"]:
            try:
                verification = verify_subscription_restored(
                    before, after, owned_codes
                )
            except Exception:
                verification = {
                    "status": "partial",
                    "checks": {},
                    "error": "verification_exception",
                }
            if (
                verification.get("status") == "restored"
                and cleanup_call_status in ("restored", "not_requested")
            ):
                cleanup_status = "restored"
            elif verification.get("status") == "failed":
                cleanup_status = "failed"
                status = FAILED
            else:
                cleanup_status = "partial"
                any_partial = True
            if cleanup_status != "restored":
                errors.append(f"subscription_cleanup:{cleanup_status}")

        try:
            context.close()
        except Exception:
            any_partial = True
            errors.append("quote_context_close_failed")
        except BaseException as exc:
            any_partial = True
            if pending_base_exception is None:
                pending_base_exception = exc
        if lock is not None:
            try:
                lock.release()
            except Exception:
                errors.append("dynamic_subscription_lock_release_failed")
                any_partial = True
            except BaseException as exc:
                any_partial = True
                if pending_base_exception is None:
                    pending_base_exception = exc

    if pending_base_exception is not None:
        raise pending_base_exception

    if any_partial and status == SUCCESS:
        status = PARTIAL
    subscription = {}
    for key in (
        "total_used",
        "remain",
        "own_used",
        "option_used_quota",
        "option_remain_quota",
        "own_option_used_quota",
    ):
        subscription[f"{key}_before"] = before.get(key)
        subscription[f"{key}_after"] = after.get(key)
    subscription["existing_quote_codes_before"] = before.get(
        "existing_quote_codes", []
    )
    subscription["existing_quote_codes_after"] = after.get(
        "existing_quote_codes", []
    )
    subscription["owned_codes"] = list(owned_codes)
    subscription["cleanup_status"] = cleanup_status
    subscription["cleanup_call_status"] = cleanup_call_status
    subscription["cleanup_wait_seconds"] = cleanup_wait_seconds
    subscription["verification"] = verification

    return sanitize_for_output(
        {
            "status": status,
            "sdk_version": sdk_version,
            "fetched_at": fetched_at,
            "market_state": market_state,
            "subscription": subscription,
            "capabilities": capabilities,
            "symbols": symbol_results,
            "errors": errors,
            "profile": config["profile"],
        }
    )
