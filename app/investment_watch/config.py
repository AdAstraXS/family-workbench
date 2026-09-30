"""Offline configuration examples; runtime reads explicit Django settings."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


@dataclass(frozen=True)
class WatchSettings:
    collect_enabled: bool = False
    model_enabled: bool = False
    monthly_model_budget_cny: Decimal = Decimal("30")
    daily_model_budget_cny: Decimal = Decimal("1")


def parse_settings(raw):
    allowed = set(WatchSettings.__dataclass_fields__)
    if set(raw) - allowed:
        raise ValueError("unknown_setting")
    values = {}
    for name in ("collect_enabled", "model_enabled"):
        value = raw.get(name, False)
        if type(value) is not bool:
            raise ValueError("boolean_required")
        values[name] = value
    for name, default in [
        ("monthly_model_budget_cny", "30"),
        ("daily_model_budget_cny", "1"),
    ]:
        try:
            value = Decimal(str(raw.get(name, default)))
        except InvalidOperation as exc:
            raise ValueError("invalid_budget") from exc
        if not value.is_finite() or value < 0:
            raise ValueError("invalid_budget")
        values[name] = value
    if values["daily_model_budget_cny"] > values["monthly_model_budget_cny"]:
        raise ValueError("daily_exceeds_monthly")
    return WatchSettings(**values)
