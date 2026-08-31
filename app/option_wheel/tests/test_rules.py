"""Core rule tests for deterministic option wheel candidate evaluation."""

from datetime import datetime, timezone
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


class SellPutRulesTest(TestCase):
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

    def test_qualified_candidate_is_executable(self):
        result = self.evaluate()
        self.assertEqual(result.status, "executable")
        self.assertEqual(result.reason_codes, ())

    def test_closed_gate_produces_investigation_only(self):
        result = self.evaluate(
            context=self.context(execution_gate_open=False)
        )
        self.assertEqual(result.status, "investigation")
        self.assertEqual(result.reason_codes, ("execution_gate_closed",))

    def test_decimal_calculations_use_conservative_bid(self):
        result = self.evaluate()
        self.assertEqual(result.required_cash, Decimal("10000"))
        self.assertEqual(result.premium_total, Decimal("500"))
        self.assertEqual(result.break_even, Decimal("95"))
        self.assertEqual(
            result.annualized_premium_rate,
            Decimal("500") / Decimal("10000") * Decimal("365") / Decimal("30"),
        )

    def test_premium_preference_uses_each_contract_not_total(self):
        result = self.evaluate(
            policy=self.policy(
                preferred_premium_min=Decimal("1000"),
                preferred_premium_max=Decimal("2000"),
            ),
            count=3,
        )
        matching_result = self.evaluate(
            policy=self.policy(
                preferred_premium_min=Decimal("400"),
                preferred_premium_max=Decimal("600"),
            ),
            count=3,
        )
        self.assertFalse(result.premium_preference_match)
        self.assertTrue(matching_result.premium_preference_match)

    def test_dte_preference_does_not_block(self):
        result = self.evaluate(quote=self.quote(dte=120))
        self.assertFalse(result.dte_preference_match)
        self.assertEqual(result.status, "executable")
        self.assertEqual(result.reason_codes, ())

    def test_cash_reservation_reduces_available_cash(self):
        account = self.account(
            settled_cash=Decimal("12000"),
            reserved_cash=Decimal("3000"),
        )
        result = self.evaluate(context=self.context(account=account))
        self.assertEqual(result.status, "blocked")
        self.assertIn("cash_insufficient", result.reason_codes)
        self.assertEqual(result.calculation_details["unreserved_cash"], "9000")

    def test_nav_limit_includes_existing_exposure(self):
        account = self.account(
            nav=Decimal("20000"),
            already_exposed_notional=Decimal("5000"),
        )
        result = self.evaluate(context=self.context(account=account))
        self.assertEqual(result.status, "blocked")
        self.assertIn("nav_ratio", result.reason_codes)
        self.assertEqual(result.calculation_details["assignment_exposure"], "15000")

    def test_provider_physical_settlement_is_accepted(self):
        result = self.evaluate(
            quote=self.quote(
                settlement_mode="PHYSICAL",
                settlement_evidence="PROVIDER_PHYSICAL",
            )
        )
        self.assertEqual(result.status, "executable")
        self.assertNotIn("settlement", result.reason_codes)

    def test_model_integer_contract_fields_are_accepted(self):
        result = self.evaluate(
            quote=self.quote(
                deliverable_shares=100,
                contract_multiplier=100,
            )
        )
        self.assertEqual(result.status, "executable")
        self.assertEqual(result.required_cash, Decimal("10000"))

    def test_occ_standard_equity_fallback_is_accepted(self):
        result = self.evaluate(
            quote=self.quote(
                settlement_mode="N/A",
                settlement_evidence="OCC_STANDARD_EQUITY",
            )
        )
        self.assertEqual(result.status, "executable")
        self.assertNotIn("settlement", result.reason_codes)

    def test_assignment_probability_boundaries_are_preserved(self):
        for probability in (Decimal("0"), Decimal("100")):
            with self.subTest(probability=probability):
                result = self.evaluate(
                    quote=self.quote(assignment_probability=probability)
                )
                self.assertEqual(result.status, "executable")
                self.assertEqual(result.assignment_probability, probability)
