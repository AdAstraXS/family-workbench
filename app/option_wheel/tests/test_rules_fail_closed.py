"""Fail-closed rule tests for malformed or incomplete wheel inputs."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest import TestCase

from option_wheel.rules import (
    AccountContext,
    EvaluationContext,
    PolicyInput,
    QuoteInput,
    evaluate_sell_put,
)


NOW = datetime(2026, 6, 16, 12, 0, tzinfo=timezone.utc)
RECENT = datetime(2026, 6, 16, 11, 59, tzinfo=timezone.utc)


class FailClosedRulesTest(TestCase):
    def account(self, **overrides):
        values = {
            "data_status": "COMPLETE",
            "currency": "USD",
            "source_as_of": RECENT,
            "uses_margin": False,
            "margin_loan_balance": Decimal("0"),
            "nav": Decimal("100000"),
            "settled_cash": Decimal("50000"),
            "reserved_cash": Decimal("0"),
            "already_exposed_notional": Decimal("0"),
        }
        values.update(overrides)
        return AccountContext(**values)

    def quote(self, **overrides):
        values = {
            "option_type": "PUT",
            "currency": "USD",
            "dte": 30,
            "strike": Decimal("100"),
            "standard_status": "STANDARD",
            "is_adjusted": False,
            "index_option_type": "N/A",
            "underlying_asset_type": "STOCK",
            "underlying_market": "US",
            "exercise_style": "AMERICAN",
            "settlement_mode": "PHYSICAL",
            "settlement_evidence": "PROVIDER_PHYSICAL",
            "deliverable_shares": Decimal("100"),
            "contract_multiplier": Decimal("100"),
            "bid": Decimal("5"),
            "ask": Decimal("5.5"),
            "volume": 1000,
            "open_interest": 500,
            "assignment_probability": Decimal("50"),
            "data_quality": "COMPLETE",
            "delay_status": "REAL_TIME",
            "freshness_status": "FRESH",
            "quote_as_of": RECENT,
            "market_session": "REGULAR",
            "regular_session_verified": True,
        }
        values.update(overrides)
        return QuoteInput(**values)

    def policy(self, **overrides):
        values = {
            "enabled": True,
            "preferred_premium_min": Decimal("1"),
            "preferred_premium_max": Decimal("20"),
            "preferred_dte_min": 7,
            "preferred_dte_max": 90,
            "max_underlying_nav_ratio": Decimal("0.5"),
            "max_spread_ratio": Decimal("0.1"),
            "min_open_interest": 1,
            "min_volume": 1,
            "account_snapshot_max_age_minutes": 60,
            "quote_max_age_seconds": 300,
            "ruleset_version": "m1-v1",
        }
        values.update(overrides)
        return PolicyInput(**values)

    def context(self, **overrides):
        values = {
            "account": self.account(),
            "event_status": "CLEAR",
            "technical_status": "COMPLETE",
            "execution_gate_open": True,
            "now": NOW,
        }
        values.update(overrides)
        return EvaluationContext(**values)

    def evaluate(self, *, context=None, quote=None, policy=None, count=1):
        return evaluate_sell_put(
            context or self.context(),
            quote or self.quote(),
            policy or self.policy(),
            contract_count=count,
        )

    def test_invalid_policy_configuration_blocks(self):
        cases = [
            {"preferred_premium_min": 1.0},
            {"preferred_premium_max": True},
            {"max_underlying_nav_ratio": None},
            {"max_spread_ratio": 0.1},
            {"preferred_dte_min": True},
            {"preferred_dte_max": None},
            {"min_open_interest": 1.0},
            {"min_volume": True},
            {"account_snapshot_max_age_minutes": None},
            {"quote_max_age_seconds": 300.0},
            {"ruleset_version": 1},
            {"max_underlying_nav_ratio": Decimal("2")},
            {"max_spread_ratio": Decimal("-0.1")},
            {"preferred_dte_min": 7, "preferred_dte_max": 3},
            {
                "preferred_premium_min": Decimal("1"),
                "preferred_premium_max": Decimal("0.5"),
            },
        ]
        for overrides in cases:
            with self.subTest(overrides=overrides):
                result = self.evaluate(policy=self.policy(**overrides))
                self.assertEqual(result.status, "blocked")
                self.assertIn("config_invalid", result.reason_codes)

    def test_account_status_currency_and_age_block(self):
        cases = [
            ({"data_status": "PARTIAL"}, "account_status"),
            ({"currency": "EUR"}, "account_currency"),
            (
                {"source_as_of": datetime(2026, 6, 16, 11, 59)},
                "account_age_invalid",
            ),
            ({"source_as_of": NOW + timedelta(minutes=5)}, "account_age_future"),
            ({"source_as_of": NOW - timedelta(hours=2)}, "account_age_expired"),
        ]
        for overrides, code in cases:
            with self.subTest(overrides=overrides):
                result = self.evaluate(
                    context=self.context(account=self.account(**overrides))
                )
                self.assertEqual(result.status, "blocked")
                self.assertIn(code, result.reason_codes)

    def test_margin_use_or_unknown_margin_data_warns(self):
        cases = [
            ({"uses_margin": "yes"}, "account_margin_status_unknown"),
            ({"uses_margin": True}, "account_margin_active"),
            ({"margin_loan_balance": None}, "account_margin_balance_unknown"),
            ({"margin_loan_balance": Decimal("50")}, "account_margin_active"),
        ]
        for overrides, code in cases:
            with self.subTest(overrides=overrides):
                result = self.evaluate(
                    context=self.context(account=self.account(**overrides))
                )
                self.assertEqual(result.status, "executable")
                self.assertIn(code, result.warning_codes)

    def test_unknown_or_invalid_nav_and_exposure_block(self):
        cases = [
            ({"nav": None}, "account_nav_missing"),
            ({"nav": Decimal("-1")}, "account_nav_nonpositive"),
            ({"already_exposed_notional": None}, "account_exposure_missing"),
            (
                {"already_exposed_notional": Decimal("-1")},
                "account_exposure_negative",
            ),
        ]
        for overrides, code in cases:
            with self.subTest(overrides=overrides):
                result = self.evaluate(
                    context=self.context(account=self.account(**overrides))
                )
                self.assertEqual(result.status, "blocked")
                self.assertIn(code, result.reason_codes)

    def test_cash_fields_are_margin_review_warnings(self):
        cases = [
            ({"settled_cash": None}, "account_cash_missing"),
            ({"settled_cash": Decimal("-5")}, "account_cash_negative"),
            ({"reserved_cash": None}, "account_reserved_missing"),
            ({"reserved_cash": Decimal("-1")}, "account_reserved_negative"),
            ({"reserved_cash": Decimal("60000")}, "account_reserved_exceeds"),
        ]
        for overrides, code in cases:
            with self.subTest(overrides=overrides):
                result = self.evaluate(
                    context=self.context(account=self.account(**overrides))
                )
                self.assertEqual(result.status, "executable")
                self.assertIn(code, result.warning_codes)

    def test_contract_count_requires_strict_positive_integer(self):
        for count in (True, 0, -1, "1", 1.0):
            with self.subTest(count=count):
                result = self.evaluate(count=count)
                self.assertEqual(result.status, "blocked")
                self.assertIn("contract_count", result.reason_codes)

    def test_put_contract_identity_fields_fail_closed(self):
        cases = [
            ({"option_type": "CALL"}, "option_type"),
            ({"standard_status": "NON_STANDARD"}, "standard"),
            ({"is_adjusted": True}, "adjusted"),
            ({"is_adjusted": "yes"}, "adjusted_status_unknown"),
            ({"index_option_type": "INDEX"}, "index"),
            ({"underlying_asset_type": "ETF"}, "asset"),
            ({"underlying_market": "EU"}, "underlying_market"),
            ({"exercise_style": "EUROPEAN"}, "exercise"),
            ({"deliverable_shares": Decimal("50")}, "deliverable"),
            ({"contract_multiplier": Decimal("50")}, "multiplier"),
        ]
        for overrides, code in cases:
            with self.subTest(overrides=overrides):
                result = self.evaluate(quote=self.quote(**overrides))
                self.assertEqual(result.status, "blocked")
                self.assertIn(code, result.reason_codes)

    def test_occ_fallback_requires_every_identity_fact(self):
        identity_failures = [
            {"standard_status": "NON_STANDARD"},
            {"is_adjusted": True},
            {"index_option_type": "INDEX"},
            {"underlying_asset_type": "ETF"},
            {"exercise_style": "EUROPEAN"},
            {"deliverable_shares": Decimal("50")},
            {"contract_multiplier": Decimal("50")},
        ]
        for overrides in identity_failures:
            with self.subTest(overrides=overrides):
                result = self.evaluate(
                    quote=self.quote(
                        settlement_mode="N/A",
                        settlement_evidence="OCC_STANDARD_EQUITY",
                        **overrides,
                    )
                )
                self.assertIn("settlement", result.reason_codes)

        for evidence in ("FUTU_PHYSICAL", "N/A", None):
            with self.subTest(evidence=evidence):
                result = self.evaluate(
                    quote=self.quote(
                        settlement_mode="N/A",
                        settlement_evidence=evidence,
                    )
                )
                self.assertIn("settlement", result.reason_codes)

    def test_quote_quality_freshness_and_market_values_fail_closed(self):
        cases = [
            ({"data_quality": "PARTIAL"}, "quote_quality", self.policy()),
            ({"delay_status": "DELAYED"}, "quote_delay", self.policy()),
            ({"freshness_status": "STALE"}, "quote_freshness", self.policy()),
            ({"market_session": "CLOSED"}, "quote_session", self.policy()),
            (
                {"regular_session_verified": False},
                "quote_session",
                self.policy(),
            ),
            (
                {"quote_as_of": datetime(2026, 6, 16, 11, 59)},
                "quote_age_invalid",
                self.policy(),
            ),
            (
                {"quote_as_of": NOW + timedelta(seconds=10)},
                "quote_age_future",
                self.policy(),
            ),
            (
                {"quote_as_of": NOW - timedelta(minutes=10)},
                "quote_age_expired",
                self.policy(),
            ),
            ({"bid": 5.0}, "quote_bid", self.policy()),
            ({"bid": Decimal("0")}, "quote_bid", self.policy()),
            ({"ask": Decimal("4")}, "quote_ask", self.policy()),
            ({"ask": 5.5}, "quote_ask", self.policy()),
            (
                {"assignment_probability": Decimal("101")},
                "quote_probability_range",
                self.policy(),
            ),
            (
                {"assignment_probability": Decimal("-1")},
                "quote_probability_range",
                self.policy(),
            ),
        ]
        for overrides, code, policy in cases:
            with self.subTest(overrides=overrides):
                result = self.evaluate(
                    quote=self.quote(**overrides), policy=policy
                )
                self.assertEqual(result.status, "blocked")
                self.assertIn(code, result.reason_codes)

    def test_liquidity_thresholds_do_not_block_or_warn(self):
        result = self.evaluate(quote=self.quote(
            ask=Decimal("7"), open_interest=0, volume=0,
        ))
        self.assertEqual(result.status, "executable")
        self.assertNotIn("quote_spread", result.warning_codes)
        self.assertNotIn("quote_open_interest", result.warning_codes)
        self.assertNotIn("quote_volume", result.warning_codes)

    def test_missing_liquidity_or_probability_fields_warn(self):
        cases = [
            ({"open_interest": 500.0}, "quote_open_interest"),
            ({"volume": 1000.0}, "quote_volume"),
            ({"assignment_probability": None}, "quote_probability_missing"),
            ({"assignment_probability": 50.0}, "quote_probability_missing"),
        ]
        for overrides, code in cases:
            with self.subTest(overrides=overrides):
                result = self.evaluate(quote=self.quote(**overrides))
                self.assertEqual(result.status, "executable")
                self.assertIn(code, result.warning_codes)

    def test_unknown_event_blocks_but_technical_analysis_warns(self):
        event = self.evaluate(context=self.context(event_status="ELEVATED"))
        self.assertEqual(event.status, "blocked")
        self.assertIn("event", event.reason_codes)
        technical = self.evaluate(context=self.context(technical_status="PARTIAL"))
        self.assertEqual(technical.status, "executable")
        self.assertIn("technical", technical.warning_codes)

    def test_malformed_values_do_not_raise_and_reasons_are_unique(self):
        result = self.evaluate(
            context=self.context(
                account=self.account(
                    data_status="PARTIAL",
                    currency="EUR",
                    nav="abc",
                    settled_cash=None,
                    reserved_cash="x",
                    already_exposed_notional=42,
                )
            ),
            quote=self.quote(
                option_type="CALL",
                strike="high",
                bid=None,
                ask="low",
                dte="thirty",
                deliverable_shares="100",
                contract_multiplier="100",
            ),
        )

        self.assertEqual(result.status, "blocked")
        expected = {
            "account_status",
            "account_currency",
            "account_nav_missing",
            "account_exposure_missing",
            "option_type",
            "strike",
            "quote_bid",
            "quote_ask",
            "dte",
            "deliverable",
            "multiplier",
        }
        self.assertTrue(expected.issubset(result.reason_codes))
        self.assertIn("account_cash_missing", result.warning_codes)
        self.assertIn("account_reserved_missing", result.warning_codes)
        self.assertEqual(len(result.reason_codes), len(set(result.reason_codes)))

    def test_non_finite_decimals_fail_closed_without_raising(self):
        cases = [
            (
                self.context(account=self.account(nav=Decimal("Infinity"))),
                self.quote(),
                self.policy(),
                "account_nav_missing",
            ),
            (
                self.context(),
                self.quote(strike=Decimal("NaN")),
                self.policy(),
                "strike",
            ),
            (
                self.context(),
                self.quote(bid=Decimal("Infinity")),
                self.policy(),
                "quote_bid",
            ),
            (
                self.context(),
                self.quote(),
                self.policy(max_underlying_nav_ratio=Decimal("Infinity")),
                "config_invalid",
            ),
            (
                self.context(),
                self.quote(strike=Decimal("1e31")),
                self.policy(),
                "strike",
            ),
        ]
        for context, quote, policy, code in cases:
            with self.subTest(code=code):
                result = self.evaluate(
                    context=context,
                    quote=quote,
                    policy=policy,
                )
                self.assertEqual(result.status, "blocked")
                self.assertIn(code, result.reason_codes)

        probability = self.evaluate(
            quote=self.quote(assignment_probability=Decimal("NaN"))
        )
        self.assertEqual(probability.status, "executable")
        self.assertIn("quote_probability_missing", probability.warning_codes)
