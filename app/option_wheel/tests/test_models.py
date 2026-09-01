"""Model tests for the option wheel M1 foundation."""

from datetime import date, datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from itertools import count

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone

from family_core.models import Family, FamilyMember
from ledger.models import BankAccount
from portfolio.models import InvestmentAccount, OptionContract, Security

from option_wheel.admin import (
    WheelBrokerAccountSnapshotAdmin,
    WheelCandidateAdmin,
    WheelDecisionAdmin,
    WheelMarketSnapshotAdmin,
    WheelOptionQuoteSnapshotAdmin,
)
from option_wheel.models import (
    DataStatus,
    DelayStatus,
    EventStatus,
    Freshness,
    OverallStatus,
    SettlementEvidence,
    StandardStatus,
    Strategy,
    TechnicalStatus,
    WheelBrokerAccountSnapshot,
    WheelCandidate,
    WheelCollateralReservation,
    WheelCycle,
    WheelDecision,
    WheelMarketSnapshot,
    WheelOptionQuoteSnapshot,
    WheelPolicy,
    WheelLeg,
    WheelTechnicalSnapshot,
    CycleStatus,
    LegStatus,
)


VERIFIED_TIME = datetime(2026, 8, 28, 15, 0, tzinfo=dt_timezone.utc)


class WheelModelTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.family = Family.objects.create(name="Wheel Family")
        cls.other_family = Family.objects.create(name="Other Family")
        cls.member = FamilyMember.objects.create(
            family=cls.family, display_name="Owner"
        )
        cls.other_member = FamilyMember.objects.create(
            family=cls.other_family, display_name="Other"
        )
        cls.other_account = cls.make_account_for_class(
            cls.other_family, cls.other_member, "other"
        )
        cls.tsla = Security.objects.create(
            symbol="TSLA",
            name="Tesla",
            market="US",
            asset_type=Security.TYPE_STOCK,
            currency="USD",
        )
        cls.tsla_put_security = Security.objects.create(
            symbol="TSLA260904P00250000",
            name="TSLA Put",
            market="US",
            asset_type=Security.TYPE_OPTION,
            currency="USD",
        )
        cls.tsla_put = OptionContract.objects.create(
            security=cls.tsla_put_security,
            underlying=cls.tsla,
            option_type=OptionContract.PUT,
            strike_price=Decimal("250"),
            expiration_date=date(2026, 9, 4),
            multiplier=100,
        )
        cls.aapl = Security.objects.create(
            symbol="AAPL",
            name="Apple",
            market="US",
            asset_type=Security.TYPE_STOCK,
            currency="USD",
        )
        cls.spy = Security.objects.create(
            symbol="SPY",
            name="SPDR",
            market="US",
            asset_type=Security.TYPE_ETF,
            currency="USD",
        )
        cls.eur_stock = Security.objects.create(
            symbol="SAP",
            name="SAP",
            market="EU",
            asset_type=Security.TYPE_STOCK,
            currency="EUR",
        )
        cls.non_us_usd_stock = Security.objects.create(
            symbol="9988",
            name="Alibaba HK",
            market="HK",
            asset_type=Security.TYPE_STOCK,
            currency="USD",
        )

    @classmethod
    def make_account_for_class(cls, family, member, suffix):
        bank = BankAccount.objects.create(
            family=family,
            member=member,
            account_name=f"Broker {suffix}",
            supports_investment=True,
        )
        return InvestmentAccount.objects.create(bank_account=bank)

    def setUp(self):
        self.sequence = count(1)

    def test_lifecycle_rejects_covered_call_below_assigned_cost(self):
        policy = self.policy()
        cycle = WheelCycle.objects.create(
            family=self.family, account=policy.account, underlying=self.tsla,
            opened_on=date(2026, 8, 1), assigned_cost_basis=Decimal("300"),
        )
        leg = WheelLeg(
            cycle=cycle, sequence=1, strategy=Strategy.COVERED_CALL,
            status=LegStatus.PLANNED, expiration=date(2026, 9, 4),
            strike=Decimal("290"),
        )
        with self.assertRaises(ValidationError):
            leg.save()

    def test_cash_collateral_requires_usd_and_positive_amount(self):
        policy = self.policy()
        cycle = WheelCycle.objects.create(
            family=self.family, account=policy.account, underlying=self.tsla,
            opened_on=date(2026, 8, 1),
        )
        leg = WheelLeg.objects.create(
            cycle=cycle, sequence=1, strategy=Strategy.SELL_PUT,
            option_contract=self.tsla_put,
            expiration=date(2026, 9, 4), strike=Decimal("250"),
        )
        reservation = WheelCollateralReservation(
            leg=leg, account=policy.account,
            kind=WheelCollateralReservation.CASH, currency="EUR",
            cash_amount=Decimal("25000"),
        )
        with self.assertRaises(ValidationError):
            reservation.save()

    def test_complete_technical_snapshot_requires_fifty_samples(self):
        snapshot = WheelTechnicalSnapshot(
            underlying=self.tsla, provider="test", source_as_of=timezone.now(),
            sample_count=49, sma_20=Decimal("1"), sma_50=Decimal("1"),
            rsi_14=Decimal("50"), atr_14=Decimal("1"),
            return_5d=Decimal("0"), return_20d=Decimal("0"),
            status=TechnicalStatus.COMPLETE,
        )
        with self.assertRaises(ValidationError):
            snapshot.save()

    def uid(self, prefix):
        return f"{prefix}-{next(self.sequence)}"

    def account(self):
        bank = BankAccount.objects.create(
            family=self.family,
            member=self.member,
            account_name=self.uid("broker"),
            supports_investment=True,
        )
        return InvestmentAccount.objects.create(bank_account=bank)

    def policy(self, *, save=True, **overrides):
        values = {
            "family": self.family,
            "account": self.account(),
            "underlying": self.tsla,
        }
        values.update(overrides)
        policy = WheelPolicy(**values)
        if save:
            policy.save()
        return policy

    def account_snapshot(self, policy, **overrides):
        values = {
            "family": policy.family,
            "account": policy.account,
            "source_kind": WheelBrokerAccountSnapshot.SOURCE_MANUAL_FILE,
            "source_as_of": timezone.now(),
        }
        values.update(overrides)
        return WheelBrokerAccountSnapshot.objects.create(**values)

    def market_snapshot(self, underlying=None, **overrides):
        values = {
            "underlying": underlying or self.tsla,
            "provider": "test",
            "source_as_of": timezone.now(),
        }
        values.update(overrides)
        return WheelMarketSnapshot.objects.create(**values)

    def quote(self, market_snapshot, *, save=True, **overrides):
        values = {
            "underlying": market_snapshot.underlying,
            "market_snapshot": market_snapshot,
            "provider": "test",
            "provider_contract_code": self.uid("contract"),
            "option_type": WheelOptionQuoteSnapshot.PUT,
            "expiration": date(2026, 9, 18),
            "strike": Decimal("200"),
            "quote_as_of": timezone.now(),
        }
        values.update(overrides)
        quote = WheelOptionQuoteSnapshot(**values)
        if save:
            quote.save()
        return quote

    def decision(self, policy=None, *, save=True, **overrides):
        policy = policy or self.policy()
        values = {
            "family": policy.family,
            "account": policy.account,
            "underlying": policy.underlying,
            "policy": policy,
            "account_snapshot": self.account_snapshot(policy),
            "market_snapshot": self.market_snapshot(policy.underlying),
            "input_fingerprint": self.uid("fingerprint"),
        }
        values.update(overrides)
        decision = WheelDecision(**values)
        if save:
            decision.save()
        return decision

    def executable_evidence(
        self,
        *,
        policy_overrides=None,
        account_overrides=None,
        market_overrides=None,
        decision_overrides=None,
    ):
        policy = self.policy(**(policy_overrides or {}))
        account_values = {
            "data_status": DataStatus.COMPLETE,
            "currency": "USD",
            "source_reference": self.uid("verified-source"),
            "settled_cash": Decimal("50000"),
            "unsettled_cash": Decimal("0"),
            "nav": Decimal("100000"),
            "reserved_cash": Decimal("0"),
            "uses_margin": False,
            "margin_loan_balance": Decimal("0"),
            "positions_summary": {},
            "open_obligations": {},
            "source_as_of": VERIFIED_TIME,
        }
        account_values.update(account_overrides or {})
        account_snapshot = self.account_snapshot(policy, **account_values)
        market_values = {
            "data_quality": DataStatus.COMPLETE,
            "delay_status": DelayStatus.REAL_TIME,
            "freshness_status": Freshness.FRESH,
            "last_price": Decimal("200"),
            "market_session": "regular",
            "regular_session_verified": True,
            "calendar_reference": "US-EQUITIES-2026-v1",
            "source_as_of": VERIFIED_TIME,
        }
        market_values.update(market_overrides or {})
        market_snapshot = self.market_snapshot(policy.underlying, **market_values)
        decision_values = {
            "execution_gate_open": True,
            "overall_status": OverallStatus.EXECUTABLE,
            "event_status": EventStatus.CLEAR,
            "technical_status": TechnicalStatus.COMPLETE,
            "decision_time": VERIFIED_TIME + timedelta(seconds=30),
            "frozen_input": {
                "already_exposed_notional": "0",
                "account_identity_verified": True,
                "event_evidence_verified": True,
                "technical_evidence_verified": True,
            },
        }
        decision_values.update(decision_overrides or {})
        decision = self.decision(
            policy,
            save=False,
            account_snapshot=account_snapshot,
            market_snapshot=market_snapshot,
            **decision_values,
        )
        return decision, market_snapshot

    def complete_quote(self, market_snapshot, **overrides):
        values = {
            "currency": "USD",
            "data_quality": DataStatus.COMPLETE,
            "delay_status": DelayStatus.REAL_TIME,
            "freshness_status": Freshness.FRESH,
            "standard_status": StandardStatus.STANDARD,
            "is_adjusted": False,
            "index_option_type": "N/A",
            "underlying_asset_type": Security.TYPE_STOCK,
            "exercise_style": "AMERICAN",
            "deliverable_shares": 100,
            "contract_multiplier": 100,
            "settlement_mode": "PHYSICAL",
            "settlement_evidence": SettlementEvidence.PROVIDER_PHYSICAL,
            "bid": Decimal("3"),
            "ask": Decimal("3.20"),
            "open_interest": 1000,
            "volume": 1000,
            "assignment_probability": Decimal("10"),
            "quote_as_of": market_snapshot.source_as_of,
        }
        values.update(overrides)
        return self.quote(market_snapshot, **values)

    def test_policy_defaults_and_valid_policy(self):
        policy = self.policy()

        self.assertTrue(policy.enabled)
        self.assertEqual(policy.preferred_premium_min, Decimal("200"))
        self.assertEqual(policy.preferred_premium_max, Decimal("400"))
        self.assertEqual((policy.preferred_dte_min, policy.preferred_dte_max), (4, 9))
        self.assertEqual(policy.max_underlying_nav_ratio, Decimal("1"))
        policy.full_clean()

    def test_policy_rejects_unsafe_scope_and_ranges(self):
        cases = [
            ({"account": self.other_account}, "account"),
            ({"underlying": self.spy}, "underlying"),
            ({"underlying": self.eur_stock}, "underlying"),
            ({"underlying": self.non_us_usd_stock}, "underlying"),
            ({"max_underlying_nav_ratio": Decimal("1.5")}, "max_underlying_nav_ratio"),
            (
                {
                    "preferred_premium_min": Decimal("500"),
                    "preferred_premium_max": Decimal("300"),
                },
                "__all__",
            ),
            ({"preferred_dte_min": 9, "preferred_dte_max": 4}, "__all__"),
        ]
        for overrides, field in cases:
            with self.subTest(overrides=overrides):
                policy = self.policy(save=False, **overrides)
                with self.assertRaises(ValidationError) as caught:
                    policy.full_clean()
                self.assertIn(field, caught.exception.message_dict)

    def test_snapshot_defaults_preserve_unknown_evidence(self):
        policy = self.policy()
        account_snapshot = self.account_snapshot(policy)
        market_snapshot = self.market_snapshot()
        quote = self.quote(market_snapshot, save=False)

        self.assertEqual(account_snapshot.data_status, DataStatus.PARTIAL)
        for value in (
            account_snapshot.settled_cash,
            account_snapshot.unsettled_cash,
            account_snapshot.nav,
            account_snapshot.reserved_cash,
            account_snapshot.margin_loan_balance,
            account_snapshot.uses_margin,
            quote.is_adjusted,
            quote.deliverable_shares,
            quote.contract_multiplier,
        ):
            self.assertIsNone(value)
        self.assertEqual(market_snapshot.delay_status, DelayStatus.UNKNOWN)
        self.assertEqual(market_snapshot.freshness_status, Freshness.UNKNOWN)
        self.assertEqual(market_snapshot.data_quality, DataStatus.PARTIAL)
        self.assertEqual(quote.standard_status, StandardStatus.UNKNOWN)

    def test_quote_probability_bounds(self):
        market_snapshot = self.market_snapshot()
        for probability in (Decimal("0"), Decimal("100")):
            with self.subTest(probability=probability):
                self.quote(
                    market_snapshot,
                    save=False,
                    assignment_probability=probability,
                ).full_clean()
        for probability in (Decimal("-0.01"), Decimal("100.01")):
            with self.subTest(probability=probability):
                quote = self.quote(
                    market_snapshot,
                    save=False,
                    assignment_probability=probability,
                )
                with self.assertRaises(ValidationError):
                    quote.full_clean()

    def test_decision_defaults_are_not_executable(self):
        decision = self.decision()

        self.assertFalse(decision.execution_gate_open)
        self.assertEqual(decision.overall_status, OverallStatus.INVESTIGATION)
        self.assertEqual(decision.event_status, EventStatus.UNKNOWN)
        self.assertEqual(decision.technical_status, TechnicalStatus.UNKNOWN)
        decision.full_clean()

        forced_decision, _ = self.executable_evidence()
        with self.assertRaises(ValidationError) as caught:
            forced_decision.full_clean()
        self.assertIn("execution_gate_open", caught.exception.message_dict)

    @override_settings(OPTION_WHEEL_EXECUTION_ENABLED=True)
    def test_executable_decision_rejects_incomplete_evidence(self):
        cases = [
            ({"enabled": False}, None, None, None, "policy"),
            (None, None, None, {"event_status": EventStatus.UNKNOWN}, "event_status"),
            (
                None,
                None,
                None,
                {"technical_status": TechnicalStatus.UNKNOWN},
                "technical_status",
            ),
            (None, {"settled_cash": None}, None, None, "account_snapshot"),
            (None, {"open_obligations": None}, None, None, "account_snapshot"),
            (None, {"source_reference": ""}, None, None, "account_snapshot"),
            (
                None,
                {"source_as_of": VERIFIED_TIME - timedelta(days=2)},
                None,
                None,
                "account_snapshot",
            ),
            (None, {"uses_margin": True}, None, None, "account_snapshot"),
            (None, None, {"data_quality": DataStatus.PARTIAL}, None, "market_snapshot"),
            (None, None, {"market_session": ""}, None, "market_snapshot"),
            (
                None,
                None,
                {"regular_session_verified": False},
                None,
                "market_snapshot",
            ),
            (
                None,
                None,
                {"calendar_reference": ""},
                None,
                "market_snapshot",
            ),
            (
                None,
                None,
                {"source_as_of": VERIFIED_TIME - timedelta(minutes=5)},
                None,
                "market_snapshot",
            ),
            (None, None, None, {"blockers": ["blocked"]}, "blockers"),
            (None, None, None, {"frozen_input": {}}, "frozen_input"),
        ]
        for policy_values, account_values, market_values, decision_values, field in cases:
            with self.subTest(field=field, decision_values=decision_values):
                decision, _ = self.executable_evidence(
                    policy_overrides=policy_values,
                    account_overrides=account_values,
                    market_overrides=market_values,
                    decision_overrides=decision_values,
                )
                with self.assertRaises(ValidationError) as caught:
                    decision.full_clean()
                self.assertIn(field, caught.exception.message_dict)

    @override_settings(OPTION_WHEEL_EXECUTION_ENABLED=True)
    def test_executable_decision_accepts_complete_cash_evidence(self):
        decision, _ = self.executable_evidence()
        decision.full_clean()

    def test_wait_and_non_wait_candidate_relationships(self):
        decision = self.decision()
        wait = WheelCandidate(
            decision=decision,
            candidate_key=self.uid("wait"),
            strategy=Strategy.WAIT,
        )
        wait.full_clean()

        no_quote = WheelCandidate(
            decision=decision,
            candidate_key=self.uid("put"),
            strategy=Strategy.SELL_PUT,
            contract_count=0,
        )
        with self.assertRaises(ValidationError) as caught:
            no_quote.full_clean()
        self.assertIn("option_quote", caught.exception.message_dict)
        self.assertIn("contract_count", caught.exception.message_dict)

        other_market = self.market_snapshot(self.aapl)
        other_quote = self.quote(other_market)
        wrong_underlying = WheelCandidate(
            decision=decision,
            option_quote=other_quote,
            candidate_key=self.uid("put"),
            strategy=Strategy.SELL_PUT,
        )
        with self.assertRaises(ValidationError) as caught:
            wrong_underlying.full_clean()
        self.assertIn("option_quote", caught.exception.message_dict)

    @override_settings(OPTION_WHEEL_EXECUTION_ENABLED=True)
    def test_executable_candidate_requires_complete_contract_evidence(self):
        decision, market_snapshot = self.executable_evidence()
        decision.full_clean()
        decision.save()
        quote = self.complete_quote(market_snapshot)
        dte = (quote.expiration - decision.decision_time.date()).days
        annualized_rate = (
            Decimal("300")
            / Decimal("20000")
            * Decimal("365")
            / Decimal(dte)
        ).quantize(Decimal("0.00000001"))
        calculations = {
            "required_cash": Decimal("20000"),
            "premium_total": Decimal("300"),
            "break_even": Decimal("197"),
            "annualized_premium_rate": annualized_rate,
            "assignment_probability": Decimal("10"),
        }
        candidate = WheelCandidate(
            decision=decision,
            option_quote=quote,
            candidate_key=self.uid("put"),
            strategy=Strategy.SELL_PUT,
            status=OverallStatus.EXECUTABLE,
            **calculations,
        )
        candidate.full_clean()

        oversized = WheelCandidate(
            decision=decision,
            option_quote=quote,
            candidate_key=self.uid("oversized"),
            strategy=Strategy.SELL_PUT,
            status=OverallStatus.EXECUTABLE,
            contract_count=1000,
            required_cash=Decimal("20000000"),
            premium_total=Decimal("300000"),
            break_even=Decimal("197"),
            annualized_premium_rate=annualized_rate,
            assignment_probability=Decimal("10"),
        )
        with self.assertRaises(ValidationError) as caught:
            oversized.full_clean()
        self.assertIn("required_cash", caught.exception.message_dict)

        invalid_cases = [
            ({"exclusion_reasons": ["excluded"]}, "exclusion_reasons"),
            ({"annualized_premium_rate": None}, "annualized_premium_rate"),
        ]
        for overrides, field in invalid_cases:
            with self.subTest(field=field):
                values = {**calculations, **overrides}
                invalid = WheelCandidate(
                    decision=decision,
                    option_quote=quote,
                    candidate_key=self.uid("invalid"),
                    strategy=Strategy.SELL_PUT,
                    status=OverallStatus.EXECUTABLE,
                    **values,
                )
                with self.assertRaises(ValidationError) as caught:
                    invalid.full_clean()
                self.assertIn(field, caught.exception.message_dict)

        incomplete_quote = self.complete_quote(
            market_snapshot,
            delay_status=DelayStatus.DELAYED,
            settlement_mode="N/A",
            settlement_evidence=SettlementEvidence.UNKNOWN,
            is_adjusted=None,
        )
        invalid = WheelCandidate(
            decision=decision,
            option_quote=incomplete_quote,
            candidate_key=self.uid("invalid-quote"),
            strategy=Strategy.SELL_PUT,
            status=OverallStatus.EXECUTABLE,
            **calculations,
        )
        with self.assertRaises(ValidationError) as caught:
            invalid.full_clean()
        self.assertIn("option_quote", caught.exception.message_dict)

        covered_call = WheelCandidate(
            decision=decision,
            option_quote=quote,
            candidate_key=self.uid("covered-call"),
            strategy=Strategy.COVERED_CALL,
            status=OverallStatus.EXECUTABLE,
            **calculations,
        )
        with self.assertRaises(ValidationError) as caught:
            covered_call.full_clean()
        self.assertIn("status", caught.exception.message_dict)

    def test_evidence_records_are_append_only_through_orm(self):
        market_snapshot = self.market_snapshot()
        market_snapshot.provider = "changed"
        with self.assertRaises(ValidationError):
            market_snapshot.save()
        with self.assertRaises(ValidationError):
            WheelMarketSnapshot.objects.filter(
                pk=market_snapshot.pk
            ).update(provider="changed")
        with self.assertRaises(ValidationError):
            WheelMarketSnapshot._base_manager.filter(
                pk=market_snapshot.pk
            ).update(provider="changed")
        with self.assertRaises(ValidationError):
            self.tsla.wheel_market_snapshots.filter(
                pk=market_snapshot.pk
            ).delete()
        with self.assertRaises(ValidationError):
            market_snapshot.delete()

    def test_evidence_admin_is_view_only(self):
        request = RequestFactory().get("/admin/")
        request.user = get_user_model().objects.create_superuser(
            username="wheel-admin",
            email="wheel@example.test",
            password="test",
        )
        pairs = [
            (WheelBrokerAccountSnapshot, WheelBrokerAccountSnapshotAdmin),
            (WheelMarketSnapshot, WheelMarketSnapshotAdmin),
            (WheelOptionQuoteSnapshot, WheelOptionQuoteSnapshotAdmin),
            (WheelDecision, WheelDecisionAdmin),
            (WheelCandidate, WheelCandidateAdmin),
        ]
        for model, admin_class in pairs:
            with self.subTest(model=model):
                model_admin = admin_class(model, admin.site)
                self.assertFalse(model_admin.has_add_permission(request))
                self.assertFalse(model_admin.has_change_permission(request))
                self.assertFalse(model_admin.has_delete_permission(request))
                self.assertIsNone(model_admin.actions)
                self.assertTrue(model_admin.has_view_permission(request))
