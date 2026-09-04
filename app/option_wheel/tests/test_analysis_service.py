from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.test import TestCase, override_settings
from django.utils import timezone

from family_core.models import Family, FamilyMember
from ledger.models import BankAccount
from portfolio.models import InvestmentAccount, Security
from option_wheel.analysis_service import persist_probe_symbol
from option_wheel.models import (
    DataStatus, OverallStatus, TechnicalStatus,
    WheelBrokerAccountSnapshot, WheelCandidate, WheelDecision,
    WheelEventSnapshot, WheelMarketSnapshot, WheelOptionQuoteSnapshot,
    WheelPolicy, WheelTechnicalSnapshot,
    WheelPause,
)


class WheelAnalysisServiceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.family = Family.objects.create(name="Analysis Family")
        member = FamilyMember.objects.create(family=cls.family, display_name="Owner")
        bank = BankAccount.objects.create(
            family=cls.family, member=member, account_name="盈透证券",
            supports_investment=True,
        )
        cls.account = InvestmentAccount.objects.create(bank_account=bank)
        cls.stock = Security.objects.create(
            symbol="TSLA", name="Tesla", market="US",
            asset_type=Security.TYPE_STOCK, currency="USD",
        )

    def metadata(self, value, as_of, status="ok"):
        return {"value": value, "as_of": as_of, "status": status}

    def probe_result(self):
        now_ny = timezone.now().astimezone(ZoneInfo("America/New_York"))
        expiration = (now_ny.date() + timedelta(days=5)).isoformat()
        as_of = now_ny.replace(microsecond=0).isoformat(sep=" ")
        history = []
        for number in range(60):
            close = Decimal("300") + Decimal(number)
            history.append({"close": str(close), "high": str(close + 2), "low": str(close - 2)})
        dynamic = {
            "bid_price": self.metadata("3.00", as_of), "ask_price": self.metadata("3.10", as_of),
            "bid_vol": self.metadata(20, as_of), "ask_vol": self.metadata(20, as_of),
            "last_price": self.metadata("3.05", as_of), "volume": self.metadata(500, as_of),
            "open_interest": self.metadata(1000, as_of), "implied_volatility": self.metadata("50", as_of),
            "delta": self.metadata("-0.2", as_of), "gamma": self.metadata("0.01", as_of),
            "theta": self.metadata("-0.1", as_of), "vega": self.metadata("0.2", as_of),
            "rho": self.metadata("-0.01", as_of), "contract_size": self.metadata(100, as_of),
            "snapshot_delay_status": self.metadata("real_time", as_of),
            "snapshot_freshness_status": self.metadata("fresh", as_of),
            "quote_delay_status": self.metadata("real_time", as_of),
            "quote_freshness_status": self.metadata("fresh", as_of),
        }
        return {
            "symbol": "US.TSLA",
            "market_state": {
                "status": "ok",
                "market_us": "MORNING",
                "timestamp": int(now_ny.timestamp()),
            },
            "underlying_quote": {"last_price": "359", "update_time": as_of, "sec_status": "NORMAL", "suspension": False},
            "history": {"status": "ok", "sample_count": 60, "last_date": str(now_ny.date()), "records": history},
            "earnings": {"status": "ok", "event_status": "clear", "query_window": {"begin": str(now_ny.date()), "end": str(now_ny.date() + timedelta(days=6))}, "records": []},
            "ex_dividend": {"status": "ok", "event_status": "clear", "query_dates": [], "records": []},
            "representative_contracts": [{
                "code": "US.TSLA_TEST_P", "option_type": "PUT", "strike_time": expiration,
                "strike_price": "320", "option_standard_type": "STANDARD", "index_option_type": "N/A",
                "option_settlement_mode": "N/A", "settlement_evidence": "occ_standard_equity",
                "deliverable_shares": 100, "exercise_style": "AMERICAN", "contract_identity_status": "ok",
                "dynamic_quote": dynamic,
                "analytics": {"probability": {"fields": {"strike_probability": {"value": "18.5"}}}},
            }],
        }

    @override_settings(OPTION_WHEEL_EXECUTION_ENABLED=False)
    def test_persists_complete_frozen_analysis_without_enabling_execution(self):
        WheelPolicy.objects.create(family=self.family, account=self.account, underlying=self.stock)
        WheelBrokerAccountSnapshot.objects.create(
            family=self.family, account=self.account,
            source_kind=WheelBrokerAccountSnapshot.SOURCE_PORTFOLIO_READONLY,
            source_reference="portfolio:test", currency="USD",
            settled_cash=Decimal("120000"), unsettled_cash=Decimal("0"), nav=Decimal("120000"),
            reserved_cash=Decimal("0"), margin_loan_balance=Decimal("0"), uses_margin=False,
            positions_summary={"items": [], "complete": True}, open_obligations={"items": [], "complete": True},
            source_as_of=timezone.now(), data_status=DataStatus.COMPLETE,
        )

        decision = persist_probe_symbol(
            family=self.family, account=self.account, symbol_result=self.probe_result(),
        )

        self.assertEqual(decision.overall_status, OverallStatus.INVESTIGATION)
        self.assertFalse(decision.execution_gate_open)
        self.assertEqual(decision.technical_status, TechnicalStatus.COMPLETE)
        self.assertEqual(WheelMarketSnapshot.objects.count(), 1)
        self.assertEqual(WheelTechnicalSnapshot.objects.count(), 1)
        self.assertEqual(WheelEventSnapshot.objects.count(), 1)
        self.assertEqual(WheelOptionQuoteSnapshot.objects.count(), 1)
        candidate = WheelCandidate.objects.get()
        self.assertEqual(candidate.status, OverallStatus.INVESTIGATION)
        self.assertEqual(candidate.exclusion_reasons, [])
        self.assertIn("execution_gate_closed", candidate.warning_reasons)
        self.assertEqual(candidate.assignment_probability, Decimal("18.5"))

    @override_settings(OPTION_WHEEL_EXECUTION_ENABLED=False)
    def test_executable_quote_freshness_uses_market_snapshot_time(self):
        WheelPolicy.objects.create(family=self.family, account=self.account, underlying=self.stock)
        WheelBrokerAccountSnapshot.objects.create(
            family=self.family, account=self.account,
            source_kind=WheelBrokerAccountSnapshot.SOURCE_PORTFOLIO_READONLY,
            source_reference="portfolio:snapshot-time", currency="USD",
            settled_cash=Decimal("120000"), unsettled_cash=Decimal("0"), nav=Decimal("120000"),
            reserved_cash=Decimal("0"), margin_loan_balance=Decimal("0"), uses_margin=False,
            positions_summary={"items": [], "complete": True}, open_obligations={"items": [], "complete": True},
            source_as_of=timezone.now(), data_status=DataStatus.COMPLETE,
        )
        result = self.probe_result()
        dynamic = result["representative_contracts"][0]["dynamic_quote"]
        stale_last_trade = (timezone.now() - timedelta(minutes=10)).astimezone(
            ZoneInfo("America/New_York")
        ).replace(microsecond=0).isoformat(sep=" ")
        for name in (
            "last_price", "volume", "open_interest", "implied_volatility",
            "delta", "gamma", "theta", "vega", "rho", "contract_size",
            "quote_delay_status", "quote_freshness_status",
        ):
            dynamic[name]["as_of"] = stale_last_trade
        dynamic["quote_delay_status"]["value"] = "delayed"
        dynamic["quote_freshness_status"]["value"] = "stale"

        decision = persist_probe_symbol(
            family=self.family, account=self.account, symbol_result=result,
        )

        option_quote = decision.candidates.get().option_quote
        expected = datetime.fromisoformat(dynamic["bid_price"]["as_of"])
        self.assertEqual(option_quote.quote_as_of, expected.astimezone(ZoneInfo("UTC")))
        self.assertEqual(option_quote.delay_status, "real_time")
        self.assertEqual(option_quote.freshness_status, "fresh")
        self.assertEqual(
            option_quote.sanitized_metadata["source_times"]["analytics_quote_as_of"],
            stale_last_trade,
        )
        self.assertNotIn("quote_age_expired", decision.candidates.get().exclusion_reasons)

    @override_settings(OPTION_WHEEL_EXECUTION_ENABLED=False)
    def test_stale_market_snapshot_still_blocks_candidate(self):
        WheelPolicy.objects.create(family=self.family, account=self.account, underlying=self.stock)
        WheelBrokerAccountSnapshot.objects.create(
            family=self.family, account=self.account,
            source_kind=WheelBrokerAccountSnapshot.SOURCE_PORTFOLIO_READONLY,
            source_reference="portfolio:stale-snapshot", currency="USD",
            settled_cash=Decimal("120000"), unsettled_cash=Decimal("0"), nav=Decimal("120000"),
            reserved_cash=Decimal("0"), margin_loan_balance=Decimal("0"), uses_margin=False,
            positions_summary={"items": [], "complete": True}, open_obligations={"items": [], "complete": True},
            source_as_of=timezone.now(), data_status=DataStatus.COMPLETE,
        )
        result = self.probe_result()
        dynamic = result["representative_contracts"][0]["dynamic_quote"]
        stale_snapshot = (timezone.now() - timedelta(minutes=10)).astimezone(
            ZoneInfo("America/New_York")
        ).replace(microsecond=0).isoformat(sep=" ")
        for name in ("bid_price", "ask_price", "bid_vol", "ask_vol"):
            dynamic[name]["as_of"] = stale_snapshot
        dynamic["snapshot_delay_status"] = self.metadata("delayed", stale_snapshot)
        dynamic["snapshot_freshness_status"] = self.metadata("stale", stale_snapshot)

        decision = persist_probe_symbol(
            family=self.family, account=self.account, symbol_result=result,
        )

        reasons = decision.candidates.get().exclusion_reasons
        self.assertIn("quote_delay", reasons)
        self.assertIn("quote_freshness", reasons)
        self.assertIn("quote_age_expired", reasons)

    @override_settings(OPTION_WHEEL_EXECUTION_ENABLED=False)
    def test_premarket_global_state_blocks_candidate(self):
        WheelPolicy.objects.create(
            family=self.family, account=self.account, underlying=self.stock
        )
        WheelBrokerAccountSnapshot.objects.create(
            family=self.family, account=self.account,
            source_kind=WheelBrokerAccountSnapshot.SOURCE_PORTFOLIO_READONLY,
            source_reference="portfolio:premarket", currency="USD",
            settled_cash=Decimal("120000"), unsettled_cash=Decimal("0"),
            nav=Decimal("120000"), reserved_cash=Decimal("0"),
            margin_loan_balance=Decimal("0"), uses_margin=False,
            positions_summary={"items": [], "complete": True},
            open_obligations={"items": [], "complete": True},
            source_as_of=timezone.now(), data_status=DataStatus.COMPLETE,
        )
        result = self.probe_result()
        result["market_state"]["market_us"] = "PRE_MARKET_BEGIN"

        decision = persist_probe_symbol(
            family=self.family, account=self.account, symbol_result=result
        )

        self.assertFalse(decision.market_snapshot.regular_session_verified)
        self.assertEqual(decision.overall_status, OverallStatus.BLOCKED)
        self.assertIn(
            "行情未通过正常交易时段实时新鲜度核验",
            decision.blockers,
        )

    @override_settings(OPTION_WHEEL_EXECUTION_ENABLED=False)
    def test_covered_call_requires_and_uses_same_account_stock_basis(self):
        WheelPolicy.objects.create(family=self.family, account=self.account, underlying=self.stock)
        WheelBrokerAccountSnapshot.objects.create(
            family=self.family, account=self.account,
            source_kind=WheelBrokerAccountSnapshot.SOURCE_PORTFOLIO_READONLY,
            source_reference="portfolio:covered", currency="USD",
            settled_cash=Decimal("20000"), unsettled_cash=Decimal("0"), nav=Decimal("120000"),
            reserved_cash=Decimal("0"), margin_loan_balance=Decimal("0"), uses_margin=False,
            positions_summary={"items": [{"symbol": "TSLA", "asset_type": "stock", "quantity": "100", "average_cost": "300", "market_value_usd": "35900"}], "complete": True},
            open_obligations={"items": [], "complete": True},
            source_as_of=timezone.now(), data_status=DataStatus.COMPLETE,
        )
        result = self.probe_result()
        contract = result["representative_contracts"][0]
        contract.update({"code": "US.TSLA_TEST_C", "option_type": "CALL", "strike_price": "310"})

        decision = persist_probe_symbol(family=self.family, account=self.account, symbol_result=result)

        candidate = decision.candidates.get()
        self.assertEqual(candidate.strategy, "covered_call")
        self.assertEqual(candidate.status, OverallStatus.INVESTIGATION)
        self.assertEqual(candidate.premium_total, Decimal("300.0000"))
        self.assertEqual(candidate.calculation_details["cost_basis"], "300")

    @override_settings(OPTION_WHEEL_EXECUTION_ENABLED=False)
    def test_covered_call_is_not_a_candidate_without_round_lot(self):
        WheelPolicy.objects.create(
            family=self.family, account=self.account, underlying=self.stock
        )
        WheelBrokerAccountSnapshot.objects.create(
            family=self.family, account=self.account,
            source_kind=WheelBrokerAccountSnapshot.SOURCE_PORTFOLIO_READONLY,
            source_reference="portfolio:no-covered-lot", currency="USD",
            settled_cash=Decimal("20000"), unsettled_cash=Decimal("0"),
            nav=Decimal("120000"), reserved_cash=Decimal("0"),
            margin_loan_balance=Decimal("0"), uses_margin=False,
            positions_summary={"items": [], "complete": True},
            open_obligations={"items": [], "complete": True},
            source_as_of=timezone.now(), data_status=DataStatus.COMPLETE,
        )
        result = self.probe_result()
        result["representative_contracts"][0].update({
            "code": "US.TSLA_TEST_C", "option_type": "CALL",
            "strike_price": "310",
        })

        decision = persist_probe_symbol(
            family=self.family, account=self.account, symbol_result=result
        )

        candidate = decision.candidates.get()
        self.assertEqual(candidate.strategy, "wait")
        self.assertEqual(candidate.exclusion_reasons, ["no_valid_contract"])
        self.assertFalse(WheelOptionQuoteSnapshot.objects.exists())

    def test_active_pause_blocks_new_candidate(self):
        WheelPolicy.objects.create(family=self.family, account=self.account, underlying=self.stock)
        WheelPause.objects.create(family=self.family, reason="市场波动异常")
        WheelBrokerAccountSnapshot.objects.create(
            family=self.family, account=self.account,
            source_kind=WheelBrokerAccountSnapshot.SOURCE_PORTFOLIO_READONLY,
            source_reference="portfolio:paused", currency="USD",
            settled_cash=Decimal("120000"), unsettled_cash=Decimal("0"), nav=Decimal("120000"),
            reserved_cash=Decimal("0"), margin_loan_balance=Decimal("0"), uses_margin=False,
            positions_summary={"items": [], "complete": True}, open_obligations={"items": [], "complete": True},
            source_as_of=timezone.now(), data_status=DataStatus.COMPLETE,
        )
        decision = persist_probe_symbol(family=self.family, account=self.account, symbol_result=self.probe_result())
        self.assertEqual(decision.overall_status, OverallStatus.BLOCKED)
        self.assertIn("策略已暂停：市场波动异常", decision.blockers)
        self.assertIn("策略已暂停：市场波动异常", decision.candidates.get().exclusion_reasons)
