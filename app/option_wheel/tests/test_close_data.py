from copy import deepcopy
from datetime import date, datetime, timedelta
from subprocess import TimeoutExpired
from unittest.mock import patch

from django.test import SimpleTestCase

from option_wheel.close_data import (
    CloseDataError, NY, calendar_target, collect, daily_row, fetch_close_report, technical_summary,
)

TARGET = date(2026, 9, 2)
NOW = datetime(2026, 9, 3, 8, tzinfo=NY)
CODE = "US.TSLA260909P350000"


class FakeQuote:
    def __init__(self):
        self.calls = []
        self.calendar = [
            {"time": "2026-09-02", "trade_date_type": "WHOLE"},
            {"time": "2026-09-03", "trade_date_type": "WHOLE"},
        ]
        self.chain = [{
            "code": CODE, "stock_owner": "US.TSLA", "strike_time": "2026-09-09",
            "strike_price": "350", "option_type": "PUT", "option_standard_type": "STANDARD",
            "lot_size": 100, "suspension": False, "option_settlement_mode": "PHYSICAL",
        }]
        self.option_history = [{"code": CODE, "time_key": "2026-09-02 00:00:00", "close": "3.20", "volume": "12"}]
        self.analytics = {"timestamp_str": "2026-09-02", "timestamp": int(datetime(2026, 9, 2, tzinfo=NY).timestamp()),
                          "strike_probability": "25.25", "implied_volatility": "40.1", "history_volatility": "50.2"}
        self.stock_history = [
            {"code": "US.TSLA", "time_key": str(TARGET - timedelta(days=i)) + " 00:00:00", "close": "357.01", "volume": 1000}
            for i in reversed(range(60))
        ]

    def request_trading_days(self, **kwargs):
        self.calls.append("calendar")
        return 0, self.calendar

    def request_history_kline(self, **kwargs):
        assert kwargs["autype"] == "None"  # Actual SDK AuType.NONE, case-sensitive.
        self.calls.append("history")
        return 0, self.stock_history if kwargs["code"] == "US.TSLA" else self.option_history, None

    def get_option_chain(self, **kwargs):
        self.calls.append("chain")
        return 0, self.chain

    def get_option_exercise_probability(self, **kwargs):
        return 0, [self.analytics]

    def get_option_volatility(self, **kwargs):
        return 0, [self.analytics]


class CloseDataTests(SimpleTestCase):
    def test_daily_evidence_and_no_live_quote_dependency(self):
        report = collect(FakeQuote(), "TSLA", NOW)
        item = report["candidates"][0]
        self.assertEqual(report["target_date"], "2026-09-02")
        self.assertEqual(item["close"], "3.20")
        self.assertEqual(item["probability"], "25.25")
        self.assertEqual(item["status"], "待开盘核价")
        self.assertIsNone(report["delta"])
        self.assertIs(report["execution_allowed"], False)
        self.assertNotIn("premium_total", item)

    def test_holiday_weekend_and_half_day_buffer(self):
        rows = [{"time": "2026-11-25", "trade_date_type": "WHOLE"},
                {"time": "2026-11-27", "trade_date_type": "HALF"},
                {"time": "2026-11-30", "trade_date_type": "WHOLE"}]
        for stamp, expected in [("2026-11-27T13:29:00", date(2026, 11, 25)),
                                ("2026-11-27T13:30:00", date(2026, 11, 27)),
                                ("2026-11-29T18:00:00", date(2026, 11, 27))]:
            self.assertEqual(calendar_target(rows, datetime.fromisoformat(stamp).replace(tzinfo=NY))[0], expected)

    def test_ny_dst_close_boundary(self):
        for month, utc_before, utc_after in [(7, 20, 21), (12, 21, 22)]:
            rows = [{"time": f"2026-{month:02}-01", "trade_date_type": "WHOLE"},
                    {"time": f"2026-{month:02}-02", "trade_date_type": "WHOLE"},
                    {"time": f"2026-{month:02}-03", "trade_date_type": "WHOLE"}]
            from datetime import timezone
            self.assertEqual(calendar_target(rows, datetime(2026, month, 2, utc_before, tzinfo=timezone.utc))[0].day, 1)
            self.assertEqual(calendar_target(rows, datetime(2026, month, 2, utc_after, tzinfo=timezone.utc))[0].day, 2)

    def test_unknown_calendar_and_missing_next_session_rejected(self):
        for rows in ([], [{"time": "2026-09-02", "trade_date_type": "UNKNOWN"}],
                     [{"time": "2026-09-02", "trade_date_type": "WHOLE"}]):
            with self.assertRaises(CloseDataError):
                calendar_target(rows, NOW)

    def test_missing_stock_day_does_not_forward_fill_or_query_chain(self):
        quote = FakeQuote()
        quote.stock_history.pop()
        report = collect(quote, "TSLA", NOW)
        self.assertTrue(report["issues"])
        self.assertFalse(report["candidates"])
        self.assertNotIn("chain", quote.calls)

    def test_zero_volume_missing_or_duplicate_day_excluded(self):
        for mode in ("zero", "missing", "duplicate", "wrong_code"):
            quote = FakeQuote()
            if mode == "zero": quote.option_history[0]["volume"] = 0
            if mode == "missing": quote.option_history[0]["time_key"] = "2026-09-01"
            if mode == "duplicate": quote.option_history *= 2
            if mode == "wrong_code": quote.option_history[0]["code"] = "US.OTHER"
            report = collect(quote, "TSLA", NOW)
            self.assertEqual(report["candidates"][0]["status"], "数据不足，排除")

    def test_probability_out_of_range_and_mismatched_timestamp(self):
        for value in ("NaN", "101", "-1", None):
            quote = FakeQuote()
            quote.analytics["strike_probability"] = value
            item = collect(quote, "TSLA", NOW)["candidates"][0]
            self.assertIsNone(item["probability"])
            self.assertTrue(item["reasons"])
        quote = FakeQuote()
        quote.analytics["timestamp"] -= 86400
        self.assertIsNone(daily_row([quote.analytics], TARGET, analytics=True))

    def test_nonstandard_expired_and_missing_multiplier_excluded(self):
        for field, value in (("option_standard_type", "NON_STANDARD"), ("strike_time", "2026-09-01"),
                             ("lot_size", None), ("suspension", True), ("stock_owner", "US.MSFT")):
            quote = FakeQuote()
            quote.chain[0][field] = value
            report = collect(quote, "TSLA", NOW)
            self.assertEqual(len(report["excluded"]), 1)
            self.assertFalse(report["candidates"])

    def test_sample_limit_and_duplicate_chain(self):
        quote = FakeQuote()
        template = quote.chain[0]
        quote.chain = [{**template, "code": f"US.TSLA260909P{price}000", "strike_price": price} for price in range(330, 350)]
        report = collect(quote, "TSLA", NOW)
        self.assertEqual(len(report["candidates"]), 3)
        self.assertEqual(report["unsampled_count"], 17)
        quote.chain.append(deepcopy(quote.chain[0]))
        self.assertTrue(collect(quote, "TSLA", NOW)["issues"])

    def test_technical_ignores_future_and_rejects_duplicate(self):
        rows = FakeQuote().stock_history
        rows.append({"code": "US.TSLA", "time_key": "2026-09-03", "close": "99999"})
        self.assertEqual(technical_summary(rows, TARGET, "US.TSLA")["sma20"], "357.0100")
        rows.append(rows[0])
        self.assertNotIn("sma20", technical_summary(rows, TARGET, "US.TSLA"))

    def test_technical_calendar_gap_not_silently_skipped(self):
        rows = FakeQuote().stock_history
        sessions = [TARGET - timedelta(days=i) for i in range(60)]
        del rows[-10]
        self.assertNotIn("sma20", technical_summary(rows, TARGET, "US.TSLA", sessions))

    @patch("option_wheel.close_data.subprocess.run")
    def test_total_process_timeout_is_bounded_and_no_sdk_text_leaks(self, run):
        run.side_effect = TimeoutExpired("child", 80, stderr="secret")
        with self.assertRaisesMessage(CloseDataError, "80 秒"):
            fetch_close_report("TSLA")
        self.assertEqual(run.call_args.kwargs["timeout"], 80)
        run.side_effect = None
        run.return_value.returncode = 1
        run.return_value.stdout = "secret API credential"
        with self.assertRaises(CloseDataError) as error:
            fetch_close_report("TSLA")
        self.assertNotIn("secret", str(error.exception))
