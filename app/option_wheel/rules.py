"""Deterministic, Decimal-only option wheel eligibility rules."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


POLICY_DISABLED = "policy_disabled"
CONFIG_INVALID = "config_invalid"

ACCOUNT_STATUS = "account_status"
ACCOUNT_CURRENCY = "account_currency"
ACCOUNT_AGE_INVALID = "account_age_invalid"
ACCOUNT_AGE_FUTURE = "account_age_future"
ACCOUNT_AGE_EXPIRED = "account_age_expired"
ACCOUNT_MARGIN_STATUS_UNKNOWN = "account_margin_status_unknown"
ACCOUNT_MARGIN_BALANCE_UNKNOWN = "account_margin_balance_unknown"
ACCOUNT_MARGIN_ACTIVE = "account_margin_active"
ACCOUNT_NAV_MISSING = "account_nav_missing"
ACCOUNT_NAV_NONPOSITIVE = "account_nav_nonpositive"
ACCOUNT_CASH_MISSING = "account_cash_missing"
ACCOUNT_CASH_NEGATIVE = "account_cash_negative"
ACCOUNT_RESERVED_MISSING = "account_reserved_missing"
ACCOUNT_RESERVED_NEGATIVE = "account_reserved_negative"
ACCOUNT_RESERVED_EXCEEDS = "account_reserved_exceeds"
ACCOUNT_EXPOSURE_MISSING = "account_exposure_missing"
ACCOUNT_EXPOSURE_NEGATIVE = "account_exposure_negative"

CONTRACT_COUNT = "contract_count"
OPTION_TYPE = "option_type"
DTE = "dte"
STRIKE = "strike"
STANDARD = "standard"
ADJUSTED_STATUS_UNKNOWN = "adjusted_status_unknown"
ADJUSTED = "adjusted"
INDEX = "index"
ASSET = "asset"
UNDERLYING_MARKET = "underlying_market"
EXERCISE = "exercise"
DELIVERABLE = "deliverable"
MULTIPLIER = "multiplier"
SETTLEMENT = "settlement"

QUOTE_CURRENCY = "quote_currency"
QUOTE_QUALITY = "quote_quality"
QUOTE_DELAY = "quote_delay"
QUOTE_FRESHNESS = "quote_freshness"
QUOTE_AGE_INVALID = "quote_age_invalid"
QUOTE_AGE_FUTURE = "quote_age_future"
QUOTE_AGE_EXPIRED = "quote_age_expired"
QUOTE_SESSION = "quote_session"
QUOTE_BID = "quote_bid"
QUOTE_ASK = "quote_ask"
QUOTE_SPREAD = "quote_spread"
QUOTE_OI = "quote_open_interest"
QUOTE_VOLUME = "quote_volume"
QUOTE_PROBABILITY_MISSING = "quote_probability_missing"
QUOTE_PROBABILITY_RANGE = "quote_probability_range"

EVENT = "event"
TECHNICAL = "technical"

CASH_INSUFFICIENT = "cash_insufficient"
NAV_RATIO = "nav_ratio"

EXECUTION_GATE_CLOSED = "execution_gate_closed"

MAX_ABS_DECIMAL = Decimal("1e30")
MAX_CONTRACT_COUNT = 1_000_000


@dataclass(frozen=True)
class AccountContext:
    data_status: object
    currency: object
    source_as_of: object
    uses_margin: object
    margin_loan_balance: object
    nav: object
    settled_cash: object
    reserved_cash: object
    already_exposed_notional: object


@dataclass(frozen=True)
class QuoteInput:
    option_type: object
    currency: object
    dte: object
    strike: object
    standard_status: object
    is_adjusted: object
    index_option_type: object
    underlying_asset_type: object
    underlying_market: object
    exercise_style: object
    settlement_mode: object
    settlement_evidence: object
    deliverable_shares: object
    contract_multiplier: object
    bid: object
    ask: object
    volume: object
    open_interest: object
    assignment_probability: object
    data_quality: object
    delay_status: object
    freshness_status: object
    quote_as_of: object
    market_session: object
    regular_session_verified: object


@dataclass(frozen=True)
class PolicyInput:
    enabled: object
    preferred_premium_min: object
    preferred_premium_max: object
    preferred_dte_min: object
    preferred_dte_max: object
    max_underlying_nav_ratio: object
    max_spread_ratio: object
    min_open_interest: object
    min_volume: object
    account_snapshot_max_age_minutes: object
    quote_max_age_seconds: object
    ruleset_version: object


@dataclass(frozen=True)
class EvaluationContext:
    account: AccountContext
    event_status: object
    technical_status: object
    execution_gate_open: object
    now: object


@dataclass(frozen=True)
class EvaluationResult:
    status: str
    reason_codes: tuple[str, ...]
    warning_codes: tuple[str, ...]
    required_cash: Decimal | None
    premium_total: Decimal | None
    break_even: Decimal | None
    annualized_premium_rate: Decimal | None
    assignment_probability: Decimal | None
    premium_preference_match: bool
    dte_preference_match: bool
    calculation_details: dict[str, str | int | bool | None]


def _is_decimal(value: object) -> bool:
    return (
        isinstance(value, Decimal)
        and value.is_finite()
        and abs(value) <= MAX_ABS_DECIMAL
    )


def _is_strict_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_hundred(value: object) -> bool:
    return (
        _is_strict_int(value) and value == 100
    ) or (
        _is_decimal(value) and value == Decimal("100")
    )


def _normalize_enum(value: object) -> str | None:
    if isinstance(value, str):
        return value.strip().upper()
    return None


def _safe_age_seconds(
    ref: object,
    now: object,
) -> tuple[str, Decimal | None]:
    if not isinstance(ref, datetime) or not isinstance(now, datetime):
        return ("invalid", None)
    try:
        ref_aware = ref.utcoffset() is not None
        now_aware = now.utcoffset() is not None
    except Exception:
        return ("invalid", None)
    if ref_aware != now_aware:
        return ("invalid", None)
    try:
        delta = now - ref
    except Exception:
        return ("invalid", None)
    total = (
        Decimal(delta.days) * Decimal(86400)
        + Decimal(delta.seconds)
        + Decimal(delta.microseconds) / Decimal(1_000_000)
    )
    if total < 0:
        return ("future", total)
    return ("ok", total)


def _json_scalar(value: object) -> str | int | bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return value
    if isinstance(value, Decimal):
        return str(value)
    return None


def evaluate_sell_put(
    context: EvaluationContext,
    quote: QuoteInput,
    policy: PolicyInput,
    contract_count: object = 1,
) -> EvaluationResult:
    reasons: list[str] = []
    warnings: list[str] = []

    if not isinstance(policy.enabled, bool) or policy.enabled is not True:
        reasons.append(POLICY_DISABLED)

    pp_min = policy.preferred_premium_min
    pp_max = policy.preferred_premium_max
    dte_min = policy.preferred_dte_min
    dte_max = policy.preferred_dte_max
    nav_ratio = policy.max_underlying_nav_ratio
    spread_limit = policy.max_spread_ratio
    min_oi = policy.min_open_interest
    min_vol = policy.min_volume
    acct_age_min = policy.account_snapshot_max_age_minutes
    quote_age_sec = policy.quote_max_age_seconds
    ruleset_version = policy.ruleset_version

    config_ok = True
    if not _is_decimal(pp_min) or pp_min < Decimal(0):
        config_ok = False
    if not _is_decimal(pp_max) or pp_max < Decimal(0):
        config_ok = False
    if (
        _is_decimal(pp_min)
        and _is_decimal(pp_max)
        and pp_max < pp_min
    ):
        config_ok = False
    if not _is_strict_int(dte_min) or dte_min < 1:
        config_ok = False
    if not _is_strict_int(dte_max) or dte_max < 1:
        config_ok = False
    if (
        _is_strict_int(dte_min)
        and _is_strict_int(dte_max)
        and dte_max < dte_min
    ):
        config_ok = False
    if not _is_decimal(nav_ratio):
        config_ok = False
    elif not (Decimal(0) < nav_ratio <= Decimal(1)):
        config_ok = False
    if not _is_decimal(spread_limit):
        config_ok = False
    elif not (Decimal(0) <= spread_limit <= Decimal(1)):
        config_ok = False
    if not _is_strict_int(min_oi) or min_oi < 0:
        config_ok = False
    if not _is_strict_int(min_vol) or min_vol < 0:
        config_ok = False
    if not _is_strict_int(acct_age_min) or acct_age_min <= 0:
        config_ok = False
    if not _is_strict_int(quote_age_sec) or quote_age_sec <= 0:
        config_ok = False
    if not isinstance(ruleset_version, str):
        config_ok = False
    if not config_ok:
        reasons.append(CONFIG_INVALID)

    account = context.account
    account_status = _normalize_enum(account.data_status)
    if account_status != "COMPLETE":
        reasons.append(ACCOUNT_STATUS)
    account_currency = _normalize_enum(account.currency)
    if account_currency != "USD":
        reasons.append(ACCOUNT_CURRENCY)

    account_age_status, account_age = _safe_age_seconds(
        account.source_as_of,
        context.now,
    )
    if account_age_status == "invalid":
        reasons.append(ACCOUNT_AGE_INVALID)
    elif account_age_status == "future":
        reasons.append(ACCOUNT_AGE_FUTURE)
    elif (
        account_age_status == "ok"
        and config_ok
        and account_age is not None
        and account_age > Decimal(acct_age_min) * Decimal(60)
    ):
        reasons.append(ACCOUNT_AGE_EXPIRED)

    if not isinstance(account.uses_margin, bool):
        warnings.append(ACCOUNT_MARGIN_STATUS_UNKNOWN)
    elif account.uses_margin is True:
        warnings.append(ACCOUNT_MARGIN_ACTIVE)

    margin_known = _is_decimal(account.margin_loan_balance)
    if not margin_known:
        warnings.append(ACCOUNT_MARGIN_BALANCE_UNKNOWN)
    elif account.margin_loan_balance != Decimal(0):
        warnings.append(ACCOUNT_MARGIN_ACTIVE)

    nav_known = _is_decimal(account.nav)
    if not nav_known:
        reasons.append(ACCOUNT_NAV_MISSING)
    elif account.nav <= Decimal(0):
        reasons.append(ACCOUNT_NAV_NONPOSITIVE)

    cash_known = _is_decimal(account.settled_cash)
    if not cash_known:
        warnings.append(ACCOUNT_CASH_MISSING)
    elif account.settled_cash < Decimal(0):
        warnings.append(ACCOUNT_CASH_NEGATIVE)

    reserved_known = _is_decimal(account.reserved_cash)
    if not reserved_known:
        warnings.append(ACCOUNT_RESERVED_MISSING)
    elif account.reserved_cash < Decimal(0):
        warnings.append(ACCOUNT_RESERVED_NEGATIVE)
    elif cash_known and account.reserved_cash > account.settled_cash:
        warnings.append(ACCOUNT_RESERVED_EXCEEDS)

    exposure_known = _is_decimal(account.already_exposed_notional)
    if not exposure_known:
        reasons.append(ACCOUNT_EXPOSURE_MISSING)
    elif account.already_exposed_notional < Decimal(0):
        reasons.append(ACCOUNT_EXPOSURE_NEGATIVE)

    count_ok = (
        _is_strict_int(contract_count)
        and 0 < contract_count <= MAX_CONTRACT_COUNT
    )
    if not count_ok:
        reasons.append(CONTRACT_COUNT)

    option_type = _normalize_enum(quote.option_type)
    if option_type != "PUT":
        reasons.append(OPTION_TYPE)

    dte_ok = _is_strict_int(quote.dte) and quote.dte > 0
    if not dte_ok:
        reasons.append(DTE)

    strike_ok = _is_decimal(quote.strike) and quote.strike > Decimal(0)
    if not strike_ok:
        reasons.append(STRIKE)

    standard_status = _normalize_enum(quote.standard_status)
    standard_ok = standard_status == "STANDARD"
    if not standard_ok:
        reasons.append(STANDARD)

    adjusted_ok = True
    if not isinstance(quote.is_adjusted, bool):
        reasons.append(ADJUSTED_STATUS_UNKNOWN)
        adjusted_ok = False
    elif quote.is_adjusted is True:
        reasons.append(ADJUSTED)
        adjusted_ok = False

    index_type = _normalize_enum(quote.index_option_type)
    index_ok = index_type == "N/A"
    if not index_ok:
        reasons.append(INDEX)

    asset_type = _normalize_enum(quote.underlying_asset_type)
    asset_ok = asset_type == "STOCK"
    if not asset_ok:
        reasons.append(ASSET)

    market = _normalize_enum(quote.underlying_market)
    market_ok = market == "US"
    if not market_ok:
        reasons.append(UNDERLYING_MARKET)

    exercise_style = _normalize_enum(quote.exercise_style)
    exercise_ok = exercise_style == "AMERICAN"
    if not exercise_ok:
        reasons.append(EXERCISE)

    deliverable_ok = _is_hundred(quote.deliverable_shares)
    if not deliverable_ok:
        reasons.append(DELIVERABLE)

    multiplier_ok = _is_hundred(quote.contract_multiplier)
    if not multiplier_ok:
        reasons.append(MULTIPLIER)

    settlement_mode = _normalize_enum(quote.settlement_mode)
    settlement_evidence = _normalize_enum(quote.settlement_evidence)
    provider_settlement = (
        settlement_mode == "PHYSICAL"
        and settlement_evidence == "PROVIDER_PHYSICAL"
    )
    fallback_settlement = (
        settlement_mode in ("N/A", "UNKNOWN", "")
        and settlement_evidence == "OCC_STANDARD_EQUITY"
        and standard_ok
        and adjusted_ok
        and index_ok
        and asset_ok
        and market_ok
        and exercise_ok
        and deliverable_ok
        and multiplier_ok
    )
    if not (provider_settlement or fallback_settlement):
        reasons.append(SETTLEMENT)

    quote_currency = _normalize_enum(quote.currency)
    if quote_currency != "USD" or quote_currency != account_currency:
        reasons.append(QUOTE_CURRENCY)
    if _normalize_enum(quote.data_quality) != "COMPLETE":
        reasons.append(QUOTE_QUALITY)
    if _normalize_enum(quote.delay_status) != "REAL_TIME":
        reasons.append(QUOTE_DELAY)
    if _normalize_enum(quote.freshness_status) != "FRESH":
        reasons.append(QUOTE_FRESHNESS)

    quote_age_status, quote_age = _safe_age_seconds(
        quote.quote_as_of,
        context.now,
    )
    if quote_age_status == "invalid":
        reasons.append(QUOTE_AGE_INVALID)
    elif quote_age_status == "future":
        reasons.append(QUOTE_AGE_FUTURE)
    elif (
        quote_age_status == "ok"
        and config_ok
        and quote_age is not None
        and quote_age > Decimal(quote_age_sec)
    ):
        reasons.append(QUOTE_AGE_EXPIRED)

    session_ok = (
        _normalize_enum(quote.market_session) == "REGULAR"
        and quote.regular_session_verified is True
        and isinstance(quote.quote_as_of, datetime)
        and quote.quote_as_of.weekday() < 5
    )
    if not session_ok:
        reasons.append(QUOTE_SESSION)

    bid_ok = _is_decimal(quote.bid) and quote.bid > Decimal(0)
    if not bid_ok:
        reasons.append(QUOTE_BID)
    ask_ok = (
        _is_decimal(quote.ask)
        and bid_ok
        and quote.ask >= quote.bid
    )
    if not ask_ok:
        reasons.append(QUOTE_ASK)

    observed_spread: Decimal | None = None
    if bid_ok and ask_ok and config_ok:
        midpoint = (quote.ask + quote.bid) / Decimal(2)
        if midpoint > Decimal(0):
            observed_spread = (quote.ask - quote.bid) / midpoint

    oi_ok = (
        _is_strict_int(quote.open_interest)
        and quote.open_interest >= 0
    )
    if not oi_ok:
        warnings.append(QUOTE_OI)

    volume_ok = _is_strict_int(quote.volume) and quote.volume >= 0
    if not volume_ok:
        warnings.append(QUOTE_VOLUME)

    probability_valid = _is_decimal(quote.assignment_probability)
    if not probability_valid:
        warnings.append(QUOTE_PROBABILITY_MISSING)
    elif not (
        Decimal(0)
        <= quote.assignment_probability
        <= Decimal(100)
    ):
        reasons.append(QUOTE_PROBABILITY_RANGE)
        probability_valid = False

    if _normalize_enum(context.event_status) != "CLEAR":
        reasons.append(EVENT)
    if _normalize_enum(context.technical_status) != "COMPLETE":
        warnings.append(TECHNICAL)

    calculation_ready = (
        count_ok
        and dte_ok
        and strike_ok
        and deliverable_ok
        and bid_ok
    )
    required_cash: Decimal | None = None
    premium_per_contract: Decimal | None = None
    premium_total: Decimal | None = None
    break_even: Decimal | None = None
    annualized: Decimal | None = None
    unreserved_cash: Decimal | None = None
    assignment_exposure: Decimal | None = None

    if calculation_ready:
        required_cash = (
            quote.strike
            * quote.deliverable_shares
            * Decimal(contract_count)
        )
        premium_per_contract = quote.bid * quote.deliverable_shares
        premium_total = premium_per_contract * Decimal(contract_count)
        break_even = quote.strike - quote.bid
        annualized = (
            premium_total
            / required_cash
            * Decimal(365)
            / Decimal(quote.dte)
        )

    if cash_known and reserved_known:
        unreserved_cash = account.settled_cash - account.reserved_cash
        if (
            calculation_ready
            and required_cash is not None
            and required_cash > unreserved_cash
        ):
            warnings.append(CASH_INSUFFICIENT)

    if exposure_known and required_cash is not None:
        assignment_exposure = (
            account.already_exposed_notional + required_cash
        )
    if (
        assignment_exposure is not None
        and nav_known
        and account.nav > Decimal(0)
        and config_ok
        and assignment_exposure > account.nav * nav_ratio
    ):
        reasons.append(NAV_RATIO)

    premium_preference_match = False
    dte_preference_match = False
    if config_ok and premium_per_contract is not None:
        premium_preference_match = (
            pp_min <= premium_per_contract <= pp_max
        )
    if config_ok and dte_ok:
        dte_preference_match = dte_min <= quote.dte <= dte_max

    reasons = list(dict.fromkeys(reasons))
    warnings = list(dict.fromkeys(warnings))
    if reasons:
        status = "blocked"
    elif (
        not isinstance(context.execution_gate_open, bool)
        or context.execution_gate_open is not True
    ):
        warnings.append(EXECUTION_GATE_CLOSED)
        status = "investigation"
    else:
        status = "executable"

    details: dict[str, str | int | bool | None] = {
        "ruleset_version": _json_scalar(ruleset_version),
        "required_cash": _json_scalar(required_cash),
        "premium_per_contract": _json_scalar(
            premium_per_contract
        ),
        "premium_total": _json_scalar(premium_total),
        "break_even": _json_scalar(break_even),
        "annualized_premium_rate": _json_scalar(annualized),
        "unreserved_cash": _json_scalar(unreserved_cash),
        "assignment_exposure": _json_scalar(assignment_exposure),
        "spread_ratio": _json_scalar(observed_spread),
        "contract_count": _json_scalar(contract_count),
        "dte": _json_scalar(quote.dte),
        "premium_preference_match": premium_preference_match,
        "dte_preference_match": dte_preference_match,
    }

    return EvaluationResult(
        status=status,
        reason_codes=tuple(reasons),
        warning_codes=tuple(warnings),
        required_cash=required_cash,
        premium_total=premium_total,
        break_even=break_even,
        annualized_premium_rate=annualized,
        assignment_probability=(
            quote.assignment_probability if probability_valid else None
        ),
        premium_preference_match=premium_preference_match,
        dte_preference_match=dte_preference_match,
        calculation_details=details,
    )
