"""Persist an auditable, analysis-only M1 decision from a sanitized Futu probe."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from zoneinfo import ZoneInfo

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from portfolio.models import Security

from .account_capacity import capacity_snapshot_stale_reasons
from .models import (
    DataStatus, DelayStatus, EventStatus, Freshness, OverallStatus,
    SettlementEvidence, StandardStatus, TechnicalStatus,
    WheelBrokerAccountSnapshot, WheelCandidate, WheelDecision,
    WheelEventSnapshot, WheelMarketRegimeSnapshot, WheelMarketSnapshot,
    WheelOptionQuoteSnapshot, WheelPolicy, WheelTechnicalSnapshot,
    WheelPause,
)
from .rules import AccountContext, EvaluationContext, PolicyInput, QuoteInput, evaluate_sell_put


NY = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")
SIX_DP = Decimal("0.000001")
EIGHT_DP = Decimal("0.00000001")
FOUR_DP = Decimal("0.0001")


class WheelAnalysisError(ValueError):
    pass


def _decimal(value):
    if value in (None, "", "N/A") or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _integer(value):
    parsed = _decimal(value)
    if parsed is None or parsed < 0 or parsed != parsed.to_integral_value():
        return None
    return int(parsed)


def _quantized(value, step):
    parsed = _decimal(value)
    return parsed.quantize(step) if parsed is not None else None


def _provider_time(value):
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        raise WheelAnalysisError("Futu 报价缺少可解析的来源时点。") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=NY)
    return parsed.astimezone(UTC)


def _field(dynamic, name):
    item = dynamic.get(name, {}) if isinstance(dynamic, dict) else {}
    return item.get("value") if isinstance(item, dict) else None


def _field_as_of(dynamic, name):
    item = dynamic.get(name, {}) if isinstance(dynamic, dict) else {}
    return item.get("as_of") if isinstance(item, dict) else None


def _history_metrics(history):
    rows = history.get("records", []) if isinstance(history, dict) else []
    cleaned = []
    for row in rows:
        close = _decimal(row.get("close"))
        high = _decimal(row.get("high"))
        low = _decimal(row.get("low"))
        if close is not None and high is not None and low is not None and high >= low:
            cleaned.append((close, high, low))
    if len(cleaned) < 50:
        return None
    closes = [row[0] for row in cleaned]
    sma20 = sum(closes[-20:], Decimal("0")) / Decimal(20)
    sma50 = sum(closes[-50:], Decimal("0")) / Decimal(50)
    changes = [closes[index] - closes[index - 1] for index in range(len(closes) - 14, len(closes))]
    gains = sum((max(value, Decimal("0")) for value in changes), Decimal("0")) / Decimal(14)
    losses = sum((max(-value, Decimal("0")) for value in changes), Decimal("0")) / Decimal(14)
    rsi = Decimal("100") if losses == 0 else Decimal("100") - Decimal("100") / (Decimal("1") + gains / losses)
    true_ranges = []
    for index in range(len(cleaned) - 14, len(cleaned)):
        close_prev = cleaned[index - 1][0]
        _, high, low = cleaned[index]
        true_ranges.append(max(high - low, abs(high - close_prev), abs(low - close_prev)))
    atr = sum(true_ranges, Decimal("0")) / Decimal(14)
    return {
        "sample_count": len(cleaned), "sma_20": sma20.quantize(SIX_DP), "sma_50": sma50.quantize(SIX_DP),
        "rsi_14": rsi.quantize(SIX_DP), "atr_14": atr.quantize(SIX_DP),
        "return_5d": (closes[-1] / closes[-6] - Decimal("1")).quantize(EIGHT_DP),
        "return_20d": (closes[-1] / closes[-21] - Decimal("1")).quantize(EIGHT_DP),
        "last_close": closes[-1],
    }


def _existing_exposure(snapshot, symbol):
    total = Decimal("0")
    summary = snapshot.positions_summary if isinstance(snapshot.positions_summary, dict) else {}
    for item in summary.get("items", []):
        if not isinstance(item, dict):
            continue
        if str(item.get("symbol", "")).upper() == symbol or str(item.get("underlying", "")).upper() == symbol:
            value = _decimal(item.get("market_value_usd"))
            if value is not None:
                total += abs(value)
    return total


def _covered_position(snapshot, symbol):
    quantity = Decimal("0")
    cost_basis = None
    summary = snapshot.positions_summary if isinstance(snapshot.positions_summary, dict) else {}
    for item in summary.get("items", []):
        if not isinstance(item, dict) or str(item.get("symbol", "")).upper() != symbol:
            continue
        if item.get("asset_type") != Security.TYPE_STOCK:
            continue
        item_quantity = _decimal(item.get("quantity"))
        item_cost = _decimal(item.get("average_cost"))
        if item_quantity is not None and item_quantity > 0:
            quantity += item_quantity
            if item_cost is not None:
                cost_basis = max(cost_basis or item_cost, item_cost)
    obligations = snapshot.open_obligations if isinstance(snapshot.open_obligations, dict) else {}
    reserved = Decimal("0")
    for item in obligations.get("items", []):
        if isinstance(item, dict) and item.get("kind") == "covered_call" and str(item.get("symbol", "")).upper() == symbol:
            reserved += _decimal(item.get("required_shares")) or Decimal("0")
    return quantity - reserved, cost_basis


def _policy_input(policy):
    return PolicyInput(
        enabled=policy.enabled,
        preferred_premium_min=policy.preferred_premium_min,
        preferred_premium_max=policy.preferred_premium_max,
        preferred_dte_min=policy.preferred_dte_min,
        preferred_dte_max=policy.preferred_dte_max,
        max_underlying_nav_ratio=policy.max_underlying_nav_ratio,
        max_spread_ratio=policy.max_spread_ratio,
        min_open_interest=policy.min_open_interest,
        min_volume=policy.min_volume,
        account_snapshot_max_age_minutes=policy.account_snapshot_max_age_minutes,
        quote_max_age_seconds=policy.quote_max_age_seconds,
        ruleset_version=policy.ruleset_version,
    )


@transaction.atomic
def persist_probe_symbol(*, family, account, symbol_result, shared_quotes=None):
    provider_symbol = str(symbol_result.get("symbol", "")).upper()
    symbol = provider_symbol.removeprefix("US.")
    try:
        underlying = Security.objects.get(symbol__iexact=symbol, market__iexact="US", asset_type=Security.TYPE_STOCK)
    except (Security.DoesNotExist, Security.MultipleObjectsReturned):
        raise WheelAnalysisError(f"{symbol} 未唯一映射到本地美股标的。") from None
    try:
        policy = WheelPolicy.objects.get(family=family, account=account, underlying=underlying, enabled=True)
    except WheelPolicy.DoesNotExist:
        raise WheelAnalysisError(f"{symbol} 尚未配置启用的账户策略。") from None
    account_snapshot = WheelBrokerAccountSnapshot.objects.filter(family=family, account=account).order_by("-source_as_of", "-pk").first()
    if account_snapshot is None:
        raise WheelAnalysisError("参与账户尚无正式容量快照。")

    quote = symbol_result.get("underlying_quote", {})
    source_as_of = _provider_time(quote.get("update_time"))
    now = timezone.now()
    quote_age = (now - source_as_of).total_seconds()
    market_state = symbol_result.get("market_state", {})
    market_us = str(market_state.get("market_us", "")).upper()
    regular = (
        market_us in {"MORNING", "AFTERNOON"}
        and str(quote.get("sec_status", "")).upper() == "NORMAL"
        and source_as_of.weekday() < 5
        and 0 <= quote_age <= policy.quote_max_age_seconds
    )
    market = WheelMarketSnapshot.objects.create(
        underlying=underlying, provider="futu", provider_symbol=provider_symbol,
        last_price=_quantized(quote.get("last_price"), SIX_DP), source_as_of=source_as_of,
        market_session="regular" if regular else "unknown",
        regular_session_verified=regular,
        calendar_reference="futu_global_state+sec_status+us_eastern_timestamp" if regular else "",
        delay_status=DelayStatus.REAL_TIME if regular else DelayStatus.UNKNOWN,
        freshness_status=Freshness.FRESH if regular else Freshness.UNKNOWN,
        data_quality=DataStatus.COMPLETE if regular else DataStatus.PARTIAL,
        sanitized_metadata={
            "market_us": market_us or None,
            "market_state_timestamp": market_state.get("timestamp"),
            "sec_status": quote.get("sec_status"),
            "suspension": quote.get("suspension"),
        },
    )

    metrics = _history_metrics(symbol_result.get("history", {}))
    technical = WheelTechnicalSnapshot.objects.create(
        underlying=underlying, provider="futu_history_qfq", source_as_of=source_as_of,
        sample_count=metrics["sample_count"] if metrics else 0,
        sma_20=metrics["sma_20"] if metrics else None, sma_50=metrics["sma_50"] if metrics else None,
        rsi_14=metrics["rsi_14"] if metrics else None, atr_14=metrics["atr_14"] if metrics else None,
        return_5d=metrics["return_5d"] if metrics else None, return_20d=metrics["return_20d"] if metrics else None,
        status=TechnicalStatus.COMPLETE if metrics else TechnicalStatus.PARTIAL,
        raw_evidence={"sample_count": symbol_result.get("history", {}).get("sample_count"), "last_date": symbol_result.get("history", {}).get("last_date")},
    )

    earnings = symbol_result.get("earnings", {})
    dividend = symbol_result.get("ex_dividend", {})
    event = WheelEventSnapshot.objects.create(
        underlying=underlying, provider="futu_calendar",
        window_start=datetime.fromisoformat(earnings["query_window"]["begin"]).date(),
        window_end=datetime.fromisoformat(earnings["query_window"]["end"]).date(),
        earnings_status=earnings.get("event_status", EventStatus.UNKNOWN),
        dividend_status=dividend.get("event_status", EventStatus.UNKNOWN),
        source_as_of=now,
        raw_evidence={"earnings_records": earnings.get("records", []), "dividend_records": dividend.get("records", []), "query_dates": dividend.get("query_dates", [])},
    )
    regime_name = "unknown"
    if metrics:
        if metrics["last_close"] > metrics["sma_20"] > metrics["sma_50"]:
            regime_name = "bullish"
        elif metrics["last_close"] < metrics["sma_20"] < metrics["sma_50"]:
            regime_name = "bearish"
        else:
            regime_name = "mixed"
    regime = WheelMarketRegimeSnapshot.objects.create(
        provider="futu_underlying_technicals", regime=regime_name, source_as_of=source_as_of,
        status=DataStatus.COMPLETE if metrics else DataStatus.PARTIAL,
        raw_evidence={"symbol": symbol, "last_close": str(metrics["last_close"]) if metrics else None},
    )

    stale_reasons = capacity_snapshot_stale_reasons(account_snapshot)
    active_pauses = WheelPause.objects.filter(
        family=family, starts_at__lte=now,
    ).filter(Q(ends_at__isnull=True) | Q(ends_at__gt=now)).filter(
        Q(account__isnull=True) | Q(account=account)
    ).filter(Q(underlying__isnull=True) | Q(underlying=underlying))
    event_status = event.overall_status
    blockers = list(stale_reasons)
    blockers.extend(f"策略已暂停：{pause.reason}" for pause in active_pauses)
    if not regular:
        blockers.append("行情未通过正常交易时段实时新鲜度核验")
    if technical.status != TechnicalStatus.COMPLETE:
        blockers.append("技术指标证据不完整")
    if event_status != EventStatus.CLEAR:
        blockers.append("财报或除息事件状态未通过")
    exposure = _existing_exposure(account_snapshot, symbol)
    frozen_input = {
        "account_identity_verified": not stale_reasons,
        "event_evidence_verified": event_status != EventStatus.UNKNOWN,
        "technical_evidence_verified": technical.status == TechnicalStatus.COMPLETE,
        "already_exposed_notional": str(exposure),
        "earnings_status": earnings.get("event_status", "unknown"),
        "earnings_as_of": now.isoformat(),
        "dividend_status": dividend.get("event_status", "unknown"),
        "dividend_as_of": now.isoformat(),
        "regime": regime.regime,
    }
    fingerprint_source = {
        "account_snapshot": account_snapshot.pk, "market": market.pk,
        "technical": technical.pk, "event": event.pk, "regime": regime.pk,
        "ruleset": policy.ruleset_version,
    }
    fingerprint = sha256(json.dumps(fingerprint_source, sort_keys=True).encode()).hexdigest()
    decision = WheelDecision.objects.create(
        family=family, account=account, underlying=underlying, policy=policy,
        account_snapshot=account_snapshot, market_snapshot=market,
        technical_snapshot=technical, event_snapshot=event, market_regime_snapshot=regime,
        decision_time=now, input_fingerprint=fingerprint,
        ruleset_version=policy.ruleset_version, event_status=event_status,
        technical_status=technical.status, execution_gate_open=False,
        overall_status=OverallStatus.BLOCKED if blockers else OverallStatus.INVESTIGATION,
        blockers=blockers, frozen_input=frozen_input,
    )

    context = EvaluationContext(
        account=AccountContext(
            data_status=account_snapshot.data_status, currency=account_snapshot.currency,
            source_as_of=account_snapshot.source_as_of, uses_margin=account_snapshot.uses_margin,
            margin_loan_balance=account_snapshot.margin_loan_balance, nav=account_snapshot.nav,
            settled_cash=account_snapshot.settled_cash, reserved_cash=account_snapshot.reserved_cash,
            already_exposed_notional=exposure,
        ),
        event_status=event_status, technical_status=technical.status,
        execution_gate_open=False, now=now,
    )
    candidates = []
    for item in symbol_result.get("representative_contracts", []):
        dynamic = item.get("dynamic_quote", {})
        # Bid/ask values come from get_market_snapshot, so their source time and
        # quality—not the last-trade time from get_stock_quote—govern whether
        # the executable quote is fresh enough for a candidate.
        quote_as_of = _provider_time(_field_as_of(dynamic, "bid_price"))
        analytics_as_of = _field_as_of(dynamic, "last_price")
        expiration = datetime.fromisoformat(str(item.get("strike_time"))[:10]).date()
        standard = str(item.get("option_standard_type", "")).upper() in {"STANDARD", "NORMAL"}
        option_type = (
            WheelOptionQuoteSnapshot.CALL
            if str(item.get("option_type", "")).upper() == "CALL"
            else WheelOptionQuoteSnapshot.PUT
        )
        option_snapshot = WheelOptionQuoteSnapshot(
            underlying=underlying, market_snapshot=market, provider="futu",
            provider_contract_code=item.get("code"), currency="USD", option_type=option_type,
            expiration=expiration, strike=_quantized(item.get("strike_price"), SIX_DP),
            standard_status=StandardStatus.STANDARD if standard else StandardStatus.UNKNOWN,
            is_adjusted=False if standard else None, index_option_type=str(item.get("index_option_type", "")),
            underlying_asset_type=Security.TYPE_STOCK, exercise_style=str(item.get("exercise_style", "")),
            settlement_mode=str(item.get("option_settlement_mode", "")),
            settlement_evidence=item.get("settlement_evidence", SettlementEvidence.UNKNOWN),
            deliverable_shares=_integer(item.get("deliverable_shares")),
            contract_multiplier=_integer(_field(dynamic, "contract_size")),
            bid=_quantized(_field(dynamic, "bid_price"), SIX_DP), ask=_quantized(_field(dynamic, "ask_price"), SIX_DP),
            bid_size=_integer(_field(dynamic, "bid_vol")), ask_size=_integer(_field(dynamic, "ask_vol")),
            last=_quantized(_field(dynamic, "last_price"), SIX_DP), volume=_integer(_field(dynamic, "volume")),
            open_interest=_integer(_field(dynamic, "open_interest")), implied_volatility=_quantized(_field(dynamic, "implied_volatility"), SIX_DP),
            delta=_quantized(_field(dynamic, "delta"), EIGHT_DP), gamma=_quantized(_field(dynamic, "gamma"), EIGHT_DP), theta=_quantized(_field(dynamic, "theta"), EIGHT_DP),
            vega=_quantized(_field(dynamic, "vega"), EIGHT_DP), rho=_quantized(_field(dynamic, "rho"), EIGHT_DP),
            assignment_probability=_quantized(item.get("analytics", {}).get("probability", {}).get("fields", {}).get("strike_probability", {}).get("value"), FOUR_DP),
            quote_as_of=quote_as_of,
            delay_status=DelayStatus.REAL_TIME if _field(dynamic, "snapshot_delay_status") == "real_time" else DelayStatus.UNKNOWN,
            freshness_status=Freshness.FRESH if _field(dynamic, "snapshot_freshness_status") == "fresh" else Freshness.UNKNOWN,
            data_quality=DataStatus.COMPLETE if item.get("contract_identity_status") == "ok" else DataStatus.PARTIAL,
            sanitized_metadata={
                "analytics": item.get("analytics", {}),
                "source_times": {
                    "executable_quote_as_of": _field_as_of(dynamic, "bid_price"),
                    "analytics_quote_as_of": analytics_as_of,
                },
            },
        )
        # Reuse only within this atomic job, never silently overwrite older evidence.
        quote_key = (option_snapshot.provider, option_snapshot.provider_contract_code, quote_as_of)
        shared = shared_quotes.get(quote_key) if shared_quotes is not None else None
        if shared is not None:
            option_snapshot.clean_fields()
            ignored = {"id", "created_at", "updated_at", "fetched_at", "market_snapshot"}
            if any(getattr(shared, f.attname) != getattr(option_snapshot, f.attname)
                   for f in option_snapshot._meta.concrete_fields if f.name not in ignored):
                raise WheelAnalysisError("同一任务的同一合约报价证据不一致，未保存分析。")
            option_snapshot = shared
        else:
            option_snapshot.save()
            if shared_quotes is not None:
                shared_quotes[quote_key] = option_snapshot
        quote_input = QuoteInput(
                option_type=option_snapshot.option_type, currency=option_snapshot.currency,
                dte=(expiration - now.astimezone(NY).date()).days, strike=option_snapshot.strike,
                standard_status=option_snapshot.standard_status, is_adjusted=option_snapshot.is_adjusted,
                index_option_type=option_snapshot.index_option_type, underlying_asset_type=option_snapshot.underlying_asset_type,
                underlying_market=underlying.market, exercise_style=option_snapshot.exercise_style,
                settlement_mode=option_snapshot.settlement_mode, settlement_evidence=option_snapshot.settlement_evidence,
                deliverable_shares=option_snapshot.deliverable_shares, contract_multiplier=option_snapshot.contract_multiplier,
                bid=option_snapshot.bid, ask=option_snapshot.ask, volume=option_snapshot.volume,
                open_interest=option_snapshot.open_interest, assignment_probability=option_snapshot.assignment_probability,
                data_quality=option_snapshot.data_quality, delay_status=option_snapshot.delay_status,
                freshness_status=option_snapshot.freshness_status, quote_as_of=option_snapshot.quote_as_of,
                market_session=market.market_session, regular_session_verified=market.regular_session_verified,
            )
        if option_type == WheelOptionQuoteSnapshot.PUT:
            result = evaluate_sell_put(context, quote_input, _policy_input(policy))
            sell_put_reasons = list(dict.fromkeys(list(result.reason_codes) + blockers))
            candidate_values = {
                "strategy": "sell_put", "status": OverallStatus.BLOCKED if blockers else result.status,
                "required_cash": _quantized(result.required_cash, FOUR_DP),
                "premium_total": _quantized(result.premium_total, FOUR_DP),
                "break_even": _quantized(result.break_even, SIX_DP),
                "annualized_premium_rate": _quantized(result.annualized_premium_rate, EIGHT_DP),
                "assignment_probability": _quantized(result.assignment_probability, FOUR_DP),
                "premium_preference_match": result.premium_preference_match,
                "dte_preference_match": result.dte_preference_match,
                "exclusion_reasons": sell_put_reasons,
                "calculation_details": result.calculation_details,
            }
        else:
            available_shares, cost_basis = _covered_position(account_snapshot, symbol)
            dte = (expiration - now.astimezone(NY).date()).days
            reasons = list(blockers)
            if available_shares < Decimal("100"):
                reasons.append("covered_shares_insufficient")
            if cost_basis is None:
                reasons.append("covered_call_cost_basis_missing")
            elif option_snapshot.strike < cost_basis:
                reasons.append("covered_call_strike_below_cost")
            if option_snapshot.data_quality != DataStatus.COMPLETE:
                reasons.append("quote_quality")
            if option_snapshot.delay_status != DelayStatus.REAL_TIME or option_snapshot.freshness_status != Freshness.FRESH:
                reasons.append("quote_not_realtime_fresh")
            if option_snapshot.bid is None or option_snapshot.bid <= 0 or option_snapshot.ask is None or option_snapshot.ask < option_snapshot.bid:
                reasons.append("quote_bid_ask")
            if option_snapshot.open_interest is None or option_snapshot.open_interest < policy.min_open_interest:
                reasons.append("quote_open_interest")
            if option_snapshot.volume is None or option_snapshot.volume < policy.min_volume:
                reasons.append("quote_volume")
            if option_snapshot.assignment_probability is None:
                reasons.append("quote_probability_missing")
            if dte <= 0:
                reasons.append("dte")
            premium = option_snapshot.bid * Decimal("100") if option_snapshot.bid is not None else None
            annualized = (
                premium / (cost_basis * Decimal("100")) * Decimal("365") / Decimal(dte)
                if premium is not None and cost_basis and dte > 0 else None
            )
            if not reasons:
                reasons.append("execution_gate_closed")
            candidate_values = {
                "strategy": "covered_call",
                "status": OverallStatus.BLOCKED if reasons != ["execution_gate_closed"] else OverallStatus.INVESTIGATION,
                "required_cash": None, "premium_total": _quantized(premium, FOUR_DP),
                "break_even": _quantized(max(cost_basis - option_snapshot.bid, Decimal("0")), SIX_DP) if cost_basis is not None and option_snapshot.bid is not None else None,
                "annualized_premium_rate": _quantized(annualized, EIGHT_DP),
                "assignment_probability": option_snapshot.assignment_probability,
                "premium_preference_match": premium is not None and policy.preferred_premium_min <= premium <= policy.preferred_premium_max,
                "dte_preference_match": policy.preferred_dte_min <= dte <= policy.preferred_dte_max,
                "exclusion_reasons": list(dict.fromkeys(reasons)),
                "calculation_details": {"covered_shares_available": str(available_shares), "cost_basis": str(cost_basis) if cost_basis is not None else None, "dte": dte},
            }
        candidates.append(WheelCandidate.objects.create(
            decision=decision, option_quote=option_snapshot,
            candidate_key=item.get("code"), **candidate_values,
        ))
    if not candidates:
        WheelCandidate.objects.create(
            decision=decision, candidate_key=f"{symbol}-wait-{fingerprint[:12]}",
            strategy="wait", status=OverallStatus.BLOCKED,
            exclusion_reasons=["no_valid_contract"],
        )
    return decision
