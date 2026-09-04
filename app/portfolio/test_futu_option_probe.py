"""Offline tests for the read-only Futu option capability probe."""

import io
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase

from portfolio.futu_option_probe import (
    FAILED,
    PARTIAL,
    ProbeLock,
    _quote_time_quality,
    probe_symbol,
    records_from,
    resolve_profile,
    run_probe,
    sanitize_for_output,
    sdk_call,
    select_representative_call,
    select_representative_put,
    subscription_summary,
    validate_symbols,
)


# Keep the dynamic-flow fixture ahead of the wall clock. A fixed 2026-09-04
# silently stopped exercising subscription and analytics once that date arrived.
DYNAMIC_EXPIRY = (datetime.now(timezone.utc).date() + timedelta(days=7)).isoformat()
from portfolio.management.commands.probe_futu_option_capabilities import (
    format_json,
    format_table,
)

COMMAND_MODULE = "portfolio.management.commands.probe_futu_option_capabilities"


class ValidateSymbolsTest(SimpleTestCase):
    def test_accepts_and_dedupes_preserving_order(self):
        self.assertEqual(
            validate_symbols(["US.TSLA", "US.MSFT", "US.TSLA"]),
            ["US.TSLA", "US.MSFT"],
        )

    def test_rejects_empty(self):
        with self.assertRaises(ValueError):
            validate_symbols([])

    def test_rejects_lowercase(self):
        with self.assertRaises(ValueError):
            validate_symbols(["us.tsla"])

    def test_rejects_non_us(self):
        with self.assertRaises(ValueError):
            validate_symbols(["HK.00700"])

    def test_rejects_empty_code(self):
        with self.assertRaises(ValueError):
            validate_symbols(["US."])

    def test_rejects_code_starting_with_punctuation(self):
        with self.assertRaises(ValueError):
            validate_symbols(["US.-TSLA"])


class QuoteTimeQualityTest(SimpleTestCase):
    def test_five_minute_old_option_quote_is_still_fresh(self):
        probe_time = datetime(2026, 9, 4, 10, 0, tzinfo=timezone.utc)
        quote_time = probe_time - timedelta(minutes=5)
        self.assertEqual(
            _quote_time_quality(quote_time.isoformat(), probe_time),
            ("real_time", "fresh"),
        )

    def test_quote_older_than_ten_minutes_is_stale(self):
        probe_time = datetime(2026, 9, 4, 10, 0, tzinfo=timezone.utc)
        quote_time = probe_time - timedelta(seconds=601)
        self.assertEqual(
            _quote_time_quality(quote_time.isoformat(), probe_time),
            ("delayed", "stale"),
        )


class ResolveProfileTest(SimpleTestCase):
    def test_static_preserves_switches(self):
        config = resolve_profile("static", True, False, True, False, True)
        self.assertEqual(config["profile"], "static")
        self.assertTrue(config["subscribe_quotes"])
        self.assertFalse(config["include_option_analytics"])
        self.assertTrue(config["include_history"])
        self.assertFalse(config["include_earnings"])
        self.assertTrue(config["allow_partial"])

    def test_m1_gate_enables_all_switches(self):
        config = resolve_profile("m1-gate", False, False, False, False, False)
        self.assertTrue(config["subscribe_quotes"])
        self.assertTrue(config["include_option_analytics"])
        self.assertTrue(config["include_history"])
        self.assertTrue(config["include_earnings"])
        self.assertFalse(config["allow_partial"])

    def test_m1_gate_rejects_allow_partial(self):
        with self.assertRaises(ValueError):
            resolve_profile("m1-gate", False, False, False, False, True)

    def test_unknown_profile_rejected(self):
        with self.assertRaises(ValueError):
            resolve_profile("turbo", False, False, False, False, False)


class SelectRepresentativePutTest(SimpleTestCase):
    def _put(self, code, strike, expiration, standard_type="STANDARD"):
        return {
            "code": code,
            "strike_price": strike,
            "expiration_date": expiration,
            "option_type": "PUT",
            "option_standard_type": standard_type,
        }

    def test_earliest_expiration_highest_strike_below_spot(self):
        records = [
            self._put("A", 200, "2026-07-15"),
            self._put("B", 220, "2026-07-15"),
            self._put("C", 210, "2026-07-15"),
            self._put("D", 190, "2026-08-15"),
        ]
        selected, metadata = select_representative_put(records, 215)
        self.assertEqual(selected["code"], "C")
        self.assertIsNone(metadata["degradation"])

    def test_no_spot_degradation(self):
        selected, metadata = select_representative_put(
            [self._put("A", 200, "2026-07-15")], None
        )
        self.assertEqual(selected["code"], "A")
        self.assertIn("spot unavailable", metadata["degradation"])

    def test_unknown_not_selected_and_counted(self):
        records = [
            self._put("X", 100, "2026-07-15", "NON_STANDARD"),
            self._put("Y", 110, "2026-07-15", ""),
            self._put("Z", 120, "2026-07-15", "STANDARD"),
        ]
        selected, metadata = select_representative_put(records, 130)
        self.assertEqual(selected["code"], "Z")
        self.assertEqual(metadata["excluded_unknown_adjustment_count"], 2)

    def test_all_unknown_returns_none(self):
        selected, metadata = select_representative_put(
            [self._put("X", 100, "2026-07-15", "NON_STANDARD")], 130
        )
        self.assertIsNone(selected)
        self.assertEqual(metadata["excluded_unknown_adjustment_count"], 1)

    def test_non_standard_not_adjusted_is_still_excluded(self):
        records = [
            {
                **self._put("X", 100, "2026-07-15", "NON_STANDARD"),
                "is_adjusted": False,
            },
            self._put("Y", 110, "2026-07-15", "STANDARD"),
        ]
        selected, metadata = select_representative_put(records, 120)
        self.assertEqual(selected["code"], "Y")
        self.assertEqual(metadata["excluded_unknown_adjustment_count"], 1)

    def test_missing_standard_type_is_excluded(self):
        records = [
            {
                "code": "X",
                "strike_price": 100,
                "expiration_date": "2026-07-15",
                "option_type": "PUT",
                "is_adjusted": False,
            },
            self._put("Y", 110, "2026-07-15", "STANDARD"),
        ]
        selected, metadata = select_representative_put(records, 120)
        self.assertEqual(selected["code"], "Y")
        self.assertEqual(metadata["excluded_unknown_adjustment_count"], 1)

    def test_explicit_standard_but_adjusted_is_excluded(self):
        records = [
            {
                **self._put("X", 100, "2026-07-15", "STANDARD"),
                "is_adjusted": True,
            },
            self._put("Y", 110, "2026-07-15", "STANDARD"),
        ]
        selected, metadata = select_representative_put(records, 120)
        self.assertEqual(selected["code"], "Y")
        self.assertEqual(metadata["excluded_unknown_adjustment_count"], 1)

    def test_adjustment_type_and_conflict_values_are_excluded(self):
        class NumpyLikeTrue:
            def item(self):
                return True

        invalid_overrides = (
            {"is_adjusted": "true"},
            {"is_adjusted": 1},
            {"is_adjusted": NumpyLikeTrue()},
            {"adjustment_status": "NON_STANDARD"},
            {"adjustment_status": "MYSTERY"},
            {"is_non_standard": True},
        )
        for overrides in invalid_overrides:
            with self.subTest(overrides=overrides):
                invalid = {
                    **self._put("X", 100, "2026-07-15"),
                    **overrides,
                }
                selected, metadata = select_representative_put(
                    [invalid, self._put("Y", 90, "2026-07-15")],
                    120,
                )
                self.assertEqual(selected["code"], "Y")
                self.assertEqual(
                    metadata["excluded_unknown_adjustment_count"], 1
                )

    def test_explicit_unadjusted_values_are_accepted(self):
        valid_overrides = (
            {"is_adjusted": False},
            {"is_adjusted": 0},
            {"is_adjusted": "false"},
            {"adjustment_status": "UNADJUSTED"},
            {"is_non_standard": False},
        )
        for overrides in valid_overrides:
            with self.subTest(overrides=overrides):
                record = {
                    **self._put("X", 100, "2026-07-15"),
                    **overrides,
                }
                selected, _ = select_representative_put([record], 120)
                self.assertEqual(selected["code"], "X")

    def test_missing_spot_uses_provider_chain_order(self):
        records = [
            self._put("FIRST", 120, "2026-07-15"),
            self._put("SECOND", 80, "2026-07-15"),
        ]
        selected, metadata = select_representative_put(records, None)
        self.assertEqual(selected["code"], "FIRST")
        self.assertIn("provider order", metadata["degradation"])

    def test_call_selection_prefers_nearest_strike_at_or_above_spot(self):
        records = []
        for code, strike in (("LOW", 190), ("ATM", 205), ("HIGH", 220)):
            record = self._put(code, strike, "2026-07-15")
            record["option_type"] = "CALL"
            records.append(record)
        selected, metadata = select_representative_call(records, 200)
        self.assertEqual(selected["code"], "ATM")
        self.assertIsNone(metadata["degradation"])


class SanitizeForOutputTest(SimpleTestCase):
    def test_masks_sensitive_dict_keys(self):
        output = sanitize_for_output(
            {
                "host": "10.0.0.1",
                "port": 11111,
                "token": "abc",
                "ok": "yes",
            }
        )
        self.assertEqual(output["host"], "***")
        self.assertEqual(output["port"], "***")
        self.assertEqual(output["token"], "***")
        self.assertEqual(output["ok"], "yes")

    def test_masks_sensitive_strings(self):
        output = sanitize_for_output(
            "host=10.0.0.1 port=11111 password=secret"
        )
        self.assertNotIn("10.0.0.1", output)
        self.assertNotIn("secret", output)

    def test_nan_becomes_none(self):
        self.assertIsNone(sanitize_for_output(float("nan")))

    def test_decimal_non_finite_values_become_none(self):
        for value in (
            Decimal("NaN"),
            Decimal("Infinity"),
            Decimal("-Infinity"),
        ):
            with self.subTest(value=value):
                self.assertIsNone(sanitize_for_output(value))

    def test_masks_compound_sensitive_keys(self):
        output = sanitize_for_output(
            {
                "broker_account_id": "12345",
                "my_access_token": "abc",
                "client_secret": "def",
                "normal_field": "visible",
            }
        )
        self.assertEqual(output["broker_account_id"], "***")
        self.assertEqual(output["my_access_token"], "***")
        self.assertEqual(output["client_secret"], "***")
        self.assertEqual(output["normal_field"], "visible")

    def test_masks_structured_connection_identity_keys(self):
        output = sanitize_for_output(
            {
                "dsn": "db-prod",
                "database_url": "postgresql://db.internal/app",
                "username": "operator",
                "user": "service-account",
                "db_user": "database-operator",
                "normal_field": "visible",
            }
        )
        for key in ("dsn", "database_url", "username", "user", "db_user"):
            self.assertEqual(output[key], "***")
        self.assertEqual(output["normal_field"], "visible")

    def test_masks_urls_and_network_endpoints(self):
        output = sanitize_for_output(
            "url=https://api.example.test/v1 "
            "ipv4=192.168.1.1:11111 ipv6=[2001:db8::1]:8080"
        )
        self.assertNotIn("api.example.test", output)
        self.assertNotIn("192.168.1.1", output)
        self.assertNotIn("2001:db8::1", output)

    def test_preserves_plain_datetime_string(self):
        value = "2026-01-01 10:05:00"
        self.assertEqual(sanitize_for_output(value), value)

    def test_masks_bare_and_quoted_credentials(self):
        output = sanitize_for_output(
            'Bearer bearer-value token token-value '
            'password "quoted value"'
        )
        self.assertNotIn("bearer-value", output)
        self.assertNotIn("token-value", output)
        self.assertNotIn("quoted value", output)

    def test_masks_labeled_host_port_and_dsns(self):
        output = sanitize_for_output(
            "host host.docker.internal port 11111 "
            "postgresql://user:pass@db.internal/app "
            "other:secret@server.internal:5432"
        )
        for secret in (
            "host.docker.internal",
            "11111",
            "user:pass",
            "db.internal",
            "other:secret",
            "server.internal",
            "5432",
        ):
            self.assertNotIn(secret, output)

    def test_preserves_plain_email_address(self):
        value = "support@example.com"
        self.assertEqual(sanitize_for_output(value), value)

    def test_datetime_and_decimal_are_json_serializable(self):
        output = sanitize_for_output(
            {
                "ts": datetime(2026, 1, 1, tzinfo=timezone.utc),
                "value": Decimal("3.14"),
            }
        )
        json.dumps(output, allow_nan=False)


class RecordsFromTest(SimpleTestCase):
    def test_fake_frame(self):
        frame = MagicMock()
        frame.to_dict.return_value = [{"a": 1}]
        self.assertEqual(records_from(frame), [{"a": 1}])

    def test_dict_wraps_list(self):
        self.assertEqual(records_from({"x": 1}), [{"x": 1}])

    def test_list_passthrough(self):
        self.assertEqual(records_from([1, 2]), [1, 2])

    def test_none_returns_empty(self):
        self.assertEqual(records_from(None), [])


class SubscriptionSummaryTest(SimpleTestCase):
    FULL_ROW = {
        "total_used": 10,
        "remain": 90,
        "own_used": 5,
        "option_used_quota": 2,
        "option_remain_quota": 8,
        "own_option_used_quota": 1,
        "sub_list": {"QUOTE": ["US.OPT1"]},
    }

    def test_missing_quota_is_unknown(self):
        row = dict(self.FULL_ROW)
        del row["own_used"]
        result = subscription_summary([row])
        self.assertEqual(result["quota_status"], "unknown")
        self.assertEqual(result["data_status"], "unknown")

    def test_non_numeric_quota_is_unknown(self):
        row = dict(self.FULL_ROW, total_used="not-a-number")
        self.assertEqual(
            subscription_summary([row])["data_status"], "unknown"
        )

    def test_negative_or_non_finite_quota_is_unknown(self):
        invalid_values = (
            -1,
            Decimal("NaN"),
            Decimal("Infinity"),
            float("nan"),
        )
        for field in self.FULL_ROW:
            if field == "sub_list":
                continue
            for value in invalid_values:
                with self.subTest(field=field, value=value):
                    row = dict(self.FULL_ROW, **{field: value})
                    result = subscription_summary([row])
                    self.assertEqual(result["quota_status"], "unknown")
                    self.assertEqual(result["data_status"], "unknown")

    def test_own_quota_cannot_exceed_global_quota(self):
        cases = (
            {"own_used": 11},
            {"own_option_used_quota": 3},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides):
                row = dict(self.FULL_ROW, **overrides)
                self.assertEqual(
                    subscription_summary([row])["data_status"],
                    "unknown",
                )

    def test_missing_subscription_list_is_unknown(self):
        row = dict(self.FULL_ROW)
        del row["sub_list"]
        result = subscription_summary([row])
        self.assertEqual(result["subscription_list_status"], "unknown")
        self.assertEqual(result["data_status"], "unknown")

    def test_malformed_quote_shapes_are_unknown(self):
        malformed_values = (
            42,
            None,
            "",
            ["US.OPT1", 42],
            ["US.OPT1", ""],
        )
        for quote_value in malformed_values:
            with self.subTest(quote_value=quote_value):
                row = dict(
                    self.FULL_ROW, sub_list={"QUOTE": quote_value}
                )
                result = subscription_summary([row])
                self.assertEqual(
                    result["subscription_list_status"], "unknown"
                )
                self.assertEqual(result["data_status"], "unknown")

    def test_list_item_without_quote_is_unknown(self):
        row = dict(self.FULL_ROW, sub_list=[{"TICKER": []}])
        self.assertEqual(
            subscription_summary([row])["data_status"], "unknown"
        )

    def test_explicit_empty_quote_list_is_valid(self):
        row = dict(self.FULL_ROW, sub_list={"QUOTE": []})
        result = subscription_summary([row])
        self.assertEqual(result["subscription_list_status"], "ok")
        self.assertEqual(result["data_status"], "ok")
        self.assertEqual(result["existing_quote_codes"], [])

    def test_quote_key_absent_means_no_quote_subscriptions(self):
        for sub_list in ({}, {"K_DAY": ["US.TSLA"]}):
            with self.subTest(sub_list=sub_list):
                row = dict(self.FULL_ROW, sub_list=sub_list)
                result = subscription_summary([row])
                self.assertEqual(result["subscription_list_status"], "ok")
                self.assertEqual(result["data_status"], "ok")
                self.assertEqual(result["existing_quote_codes"], [])

    def test_malformed_frame_never_raises(self):
        frame = MagicMock()
        frame.to_dict.side_effect = RuntimeError("boom")
        self.assertEqual(
            subscription_summary(frame)["data_status"], "unknown"
        )


class SdkCallTest(SimpleTestCase):
    def test_binary_provider_error_preserves_metadata_and_sanitizes(self):
        context = MagicMock()
        context.get_data.return_value = (
            1,
            "connect 192.168.0.1:5000 failed",
        )
        result = sdk_call(context, "get_data", 0)
        self.assertEqual(result["ret_code"], 1)
        self.assertEqual(result["category"], "provider_error")
        self.assertNotIn("192.168.0.1", result["error"])

    def test_ternary_provider_error_preserves_extra_and_sanitizes(self):
        context = MagicMock()
        context.get_data.return_value = (
            2,
            "bad token=abc",
            "endpoint=https://provider.example/error",
        )
        result = sdk_call(context, "get_data", 0)
        self.assertEqual(result["ret_code"], 2)
        self.assertEqual(result["category"], "provider_error")
        self.assertNotIn("abc", result["error"])
        self.assertNotIn("provider.example", result["error"])


class FormatTest(SimpleTestCase):
    def test_format_json_parseable(self):
        parsed = json.loads(format_json({"status": "success", "symbols": []}))
        self.assertEqual(parsed["status"], "success")

    def test_format_table_has_no_account_data(self):
        result = {
            "status": "success",
            "sdk_version": "9.9",
            "profile": "static",
            "subscription": {
                "cleanup_status": "not_requested",
                "owned_codes": [],
            },
            "symbols": [
                {
                    "symbol": "US.TSLA",
                    "status": "success",
                    "expirations": ["2026-07-15"],
                    "representative_contracts": [{"code": "X"}],
                    "errors": [],
                }
            ],
        }
        output = format_table(result)
        self.assertIn("US.TSLA", output)
        self.assertNotIn("account", output.lower())
        self.assertNotIn("token", output.lower())


class CommandTest(SimpleTestCase):
    def _result(self, status):
        return {
            "status": status,
            "sdk_version": "9.9",
            "profile": "static",
            "subscription": {
                "cleanup_status": "not_requested",
                "owned_codes": [],
            },
            "symbols": [],
            "errors": [],
        }

    def test_success_writes_output(self):
        with patch(
            f"{COMMAND_MODULE}.run_probe", return_value=self._result("success")
        ):
            output = io.StringIO()
            call_command(
                "probe_futu_option_capabilities",
                symbols=["US.TSLA"],
                stdout=output,
            )
        self.assertIn("Status: success", output.getvalue())

    def test_partial_default_raises_but_stdout_has_result(self):
        with patch(
            f"{COMMAND_MODULE}.run_probe", return_value=self._result(PARTIAL)
        ):
            output = io.StringIO()
            with self.assertRaises(CommandError):
                call_command(
                    "probe_futu_option_capabilities",
                    symbols=["US.TSLA"],
                    stdout=output,
                )
        self.assertIn("Status:", output.getvalue())

    def test_partial_allow_partial_does_not_raise(self):
        with patch(
            f"{COMMAND_MODULE}.run_probe", return_value=self._result(PARTIAL)
        ):
            output = io.StringIO()
            call_command(
                "probe_futu_option_capabilities",
                symbols=["US.TSLA"],
                allow_partial=True,
                stdout=output,
            )
        self.assertIn("Status:", output.getvalue())

    def test_failed_always_raises(self):
        with patch(
            f"{COMMAND_MODULE}.run_probe", return_value=self._result(FAILED)
        ):
            with self.assertRaises(CommandError):
                call_command(
                    "probe_futu_option_capabilities",
                    symbols=["US.TSLA"],
                    allow_partial=True,
                    stdout=io.StringIO(),
                )

    def test_json_stdout_parseable(self):
        with patch(
            f"{COMMAND_MODULE}.run_probe", return_value=self._result("success")
        ):
            output = io.StringIO()
            call_command(
                "probe_futu_option_capabilities",
                symbols=["US.TSLA"],
                format="json",
                stdout=output,
            )
        self.assertIsInstance(json.loads(output.getvalue()), dict)

    def test_command_passes_profile_and_switches(self):
        mock_probe = MagicMock(return_value=self._result("success"))
        with patch(f"{COMMAND_MODULE}.run_probe", mock_probe):
            call_command(
                "probe_futu_option_capabilities",
                symbols=["US.TSLA", "US.MSFT"],
                profile="m1-gate",
                max_expirations=2,
                max_contracts_per_expiration=3,
                subscribe_quotes=True,
                include_option_analytics=True,
                include_history=True,
                include_earnings=True,
                stdout=io.StringIO(),
            )
        kwargs = mock_probe.call_args.kwargs
        self.assertEqual(kwargs["profile"], "m1-gate")
        self.assertTrue(kwargs["subscribe_quotes"])
        self.assertTrue(kwargs["include_option_analytics"])
        self.assertTrue(kwargs["include_history"])
        self.assertTrue(kwargs["include_earnings"])
        self.assertEqual(kwargs["max_expirations"], 2)
        self.assertEqual(kwargs["max_contracts_per_expiration"], 3)


class FakeFutu:
    RET_OK = 0
    __version__ = "9.9.9"

    class OptionType:
        ALL = "ALL"

    class SubType:
        QUOTE = "QUOTE"

    class KLType:
        K_DAY = "K_DAY"

    class AuType:
        QFQ = "QFQ"

    class Market:
        US = "US"


class FakeContext:
    def __init__(
        self,
        fail_before_subscription=False,
        fail_after_subscription=False,
        fail_expiration=None,
        fail_chain=None,
        no_standard_put=None,
        symbol_exception=None,
    ):
        self.calls = []
        self.closed = False
        self.fail_before_subscription = fail_before_subscription
        self.fail_after_subscription = fail_after_subscription
        self.fail_expiration = fail_expiration or set()
        self.fail_chain = fail_chain or set()
        self.no_standard_put = no_standard_put or set()
        self.symbol_exception = symbol_exception or {}
        self.subscription_calls = 0

    def query_subscription(self, is_all_conn=False):
        self.calls.append("query_subscription")
        self.subscription_calls += 1
        if self.subscription_calls == 1 and self.fail_before_subscription:
            return 1, "before subscription error"
        if self.subscription_calls > 1 and self.fail_after_subscription:
            return 1, "after subscription error"
        return 0, {
            "total_used": 10,
            "remain": 90,
            "own_used": 5,
            "option_used_quota": 2,
            "option_remain_quota": 8,
            "own_option_used_quota": 1,
            "sub_list": {"QUOTE": ["US.OPT_EXIST"]},
        }

    def get_market_snapshot(self, codes):
        self.calls.append("get_market_snapshot")
        for code in codes:
            if code in self.symbol_exception:
                raise self.symbol_exception[code]
        return 0, [
            {
                "last_price": 200.0,
                "update_time": "2026-01-01 10:00:00",
                "sec_status": "NORMAL",
                "suspension": False,
            }
        ]

    def get_option_expiration_date(self, symbol):
        self.calls.append("get_option_expiration_date")
        if symbol in self.fail_expiration:
            return 1, "expiration failed"
        return 0, [
            {"strike_time": "2026-09-04"},
            {"strike_time": "2026-09-11"},
            {"strike_time": "2026-09-18"},
        ]

    def get_option_chain(self, symbol, start=None, end=None, option_type=None):
        self.calls.append("get_option_chain")
        if symbol in self.fail_chain:
            return 1, "chain failed"
        if symbol in self.no_standard_put:
            return 0, [
                {
                    "code": "NS",
                    "option_type": "PUT",
                    "strike_price": 100,
                    "option_standard_type": "NON_STANDARD",
                    "strike_time": start,
                },
                {
                    "code": "C1",
                    "option_type": "CALL",
                    "strike_price": 100,
                    "option_standard_type": "STANDARD",
                    "strike_time": start,
                },
            ]
        return 0, [
            {
                "code": f"{symbol}-{start}-P1",
                "option_type": "PUT",
                "strike_price": 190,
                "option_standard_type": "STANDARD",
                "strike_time": start,
                "lot_size": 100,
                "option_settlement_mode": "PHYSICAL",
            },
            {
                "code": f"{symbol}-{start}-P2",
                "option_type": "PUT",
                "strike_price": 185,
                "option_standard_type": "STANDARD",
                "strike_time": start,
                "lot_size": 100,
                "option_settlement_mode": "PHYSICAL",
            },
            {
                "code": "NS",
                "option_type": "PUT",
                "strike_price": 180,
                "option_standard_type": "NON_STANDARD",
                "strike_time": start,
            },
            {
                "code": "C1",
                "option_type": "CALL",
                "strike_price": 210,
                "option_standard_type": "STANDARD",
                "strike_time": start,
            },
        ]

    def close(self):
        self.closed = True


class RunProbeStaticTest(SimpleTestCase):
    def test_static_success_and_subscription_snapshot(self):
        context = FakeContext()
        result = run_probe(
            ["US.TSLA"],
            futu_module=FakeFutu(),
            context_factory=lambda: context,
        )
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["sdk_version"], "9.9.9")
        self.assertEqual(result["subscription"]["total_used_before"], 10)
        self.assertEqual(result["subscription"]["total_used_after"], 10)
        self.assertTrue(context.closed)
        self.assertNotIn("subscribe", context.calls)
        json.dumps(result, allow_nan=False)


class FakeLock:
    def __init__(self, acquire_result=True):
        self.acquire_result = acquire_result
        self.released = False

    def acquire(self):
        return self.acquire_result

    def release(self):
        self.released = True


class DynamicContext:
    def __init__(
        self,
        fail_subscribe=False,
        fail_unsubscribe=False,
        fail_close=False,
        fail_before=False,
        existing_codes=None,
        missing_fields=None,
    ):
        self.calls = []
        self.closed = False
        self.fail_subscribe = fail_subscribe
        self.fail_unsubscribe = fail_unsubscribe
        self.fail_close = fail_close
        self.fail_before = fail_before
        self.existing_codes = set(existing_codes or [])
        self.missing_fields = set(missing_fields or [])
        self.subscription_calls = 0

    def get_global_state(self):
        self.calls.append(("get_global_state",))
        return 0, {
            "market_us": "MORNING",
            "timestamp": 1788220800,
        }

    def query_subscription(self, is_all_conn=False):
        self.calls.append(("query_subscription", is_all_conn))
        self.subscription_calls += 1
        if self.subscription_calls == 1 and self.fail_before:
            return 1, "before subscription error"
        return 0, {
            "total_used": 10,
            "remain": 90,
            "own_used": 5,
            "option_used_quota": 2,
            "option_remain_quota": 8,
            "own_option_used_quota": 1,
            "sub_list": {"QUOTE": sorted(self.existing_codes)},
        }

    def get_market_snapshot(self, codes):
        self.calls.append(("get_market_snapshot", tuple(codes)))
        code = codes[0]
        if code in ("US.TSLA", "US.MSFT"):
            return 0, [
                {
                    "last_price": 200.0,
                    "update_time": "2026-01-01 10:00:00",
                    "sec_status": "NORMAL",
                    "suspension": False,
                }
            ]
        row = {
            "bid_price": 1.5,
            "ask_price": 1.6,
            "bid_vol": 100,
            "ask_vol": 200,
            "option_contract_size": 25,
            "option_owner_lot_multiplier": 2,
            "option_contract_multiplier": 50,
            "option_area_type": "US",
            "update_time": "2026-01-01 10:05:00",
        }
        for field in self.missing_fields:
            row.pop(field, None)
        return 0, [row]

    def get_option_expiration_date(self, symbol):
        self.calls.append(("get_option_expiration_date", symbol))
        return 0, [{"strike_time": DYNAMIC_EXPIRY}]

    def get_option_chain(self, symbol, start=None, end=None, option_type=None):
        self.calls.append(("get_option_chain", symbol))
        return 0, [
            {
                "code": f"{symbol}-{start}-P1",
                "option_type": "PUT",
                "strike_price": 190,
                "option_standard_type": "STANDARD",
                "strike_time": start,
                "expiration_date": start,
                "lot_size": 100,
                "stock_owner": symbol,
                "option_settlement_mode": "PHYSICAL",
                "index_option_type": "NORMAL",
            }
        ]

    def subscribe(self, codes, subtypes, **kwargs):
        self.calls.append(("subscribe", tuple(codes), tuple(subtypes)))
        if self.fail_subscribe:
            return 1, "quota exceeded"
        return 0, None

    def unsubscribe(self, codes, subtypes, unsubscribe_all=False):
        self.calls.append(
            ("unsubscribe", tuple(codes), tuple(subtypes), unsubscribe_all)
        )
        if self.fail_unsubscribe:
            return 1, "unsubscribe error"
        return 0, None

    def get_stock_quote(self, codes):
        self.calls.append(("get_stock_quote", tuple(codes)))
        row = {
            "data_date": "2026-01-01",
            "data_time": "10:05:00",
            "last_price": 199.5,
            "volume": 5000,
            "open_interest": 12000,
            "implied_volatility": 0.25,
            "delta": -0.45,
            "gamma": 0.02,
            "theta": -0.03,
            "vega": 0.05,
            "rho": -0.01,
            "contract_size": 25,
        }
        for field in self.missing_fields:
            row.pop(field, None)
        return 0, [row]

    def get_option_exercise_probability(self, code):
        self.calls.append(("get_option_exercise_probability", code))
        return 0, [
            {
                "timestamp": 1788220800,
                "timestamp_str": "2026-09-01",
                "security_price": 200.0,
                "strike_probability": 35.0,
            }
        ]

    def get_option_volatility(self, code):
        self.calls.append(("get_option_volatility", code))
        return 0, [
            {
                "timestamp": 1788220800,
                "timestamp_str": "2026-09-01",
                "implied_volatility": 25.0,
                "history_volatility": 22.0,
                "volatility_premium": 3.0,
                "average_impvol": 24.5,
                "impvol_status": "IMPVOL_FLUCTUATING",
                "analysis": "",
            }
        ]

    def request_history_kline(
        self, symbol, ktype=None, autype=None, max_count=None
    ):
        self.calls.append(("request_history_kline", symbol))
        return (
            0,
            [
                {"time_key": "2026-01-02", "close": 201.0},
                {"time_key": "2026-01-03", "close": 202.0},
            ],
            {"next": None},
        )

    def get_earnings_calendar(self, market, begin_date=None, end_date=None):
        self.calls.append(
            ("get_earnings_calendar", market, begin_date, end_date)
        )
        return 0, [
            {
                "security": "US.TSLA",
                "earnings_date": "2026-07-20",
                "pub_type": "AFTER_MARKET",
            }
        ]

    def get_dividend_calendar(
        self, market, date, data_from=None, count=None
    ):
        self.calls.append(("get_dividend_calendar", market, date))
        return 0, (
            1,
            [
                {
                    "security": "US.TSLA",
                    "record_date": date,
                    "ex_date": date,
                    "dividend_payable_date": date,
                }
            ],
        )

    def close(self):
        self.calls.append(("close",))
        if self.fail_close:
            raise RuntimeError("close failed")
        self.closed = True


@patch("portfolio.futu_option_probe.MIN_SUBSCRIPTION_SECONDS", 0)
class DynamicProbeTest(SimpleTestCase):
    def _run(self, context_options=None, lock_acquire=True, profile="static", **extra):
        context = DynamicContext(**(context_options or {}))
        lock = FakeLock(acquire_result=lock_acquire)
        result = run_probe(
            ["US.TSLA"],
            profile=profile,
            subscribe_quotes=extra.pop("subscribe_quotes", True),
            futu_module=FakeFutu(),
            context_factory=lambda: context,
            lock_factory=lambda: lock,
            **extra,
        )
        return result, context, lock

    def test_dynamic_success_owned_cleanup_order_and_lock(self):
        result, context, lock = self._run()
        self.assertEqual(result["status"], "partial")
        self.assertEqual(
            result["subscription"]["owned_codes"],
            [f"US.TSLA-{DYNAMIC_EXPIRY}-P1"],
        )
        self.assertEqual(
            result["subscription"]["cleanup_status"], "restored"
        )
        self.assertTrue(context.closed)
        self.assertTrue(lock.released)
        names = [call[0] for call in context.calls]
        unsubscribe_index = names.index("unsubscribe")
        second_query_index = names.index(
            "query_subscription", names.index("query_subscription") + 1
        )
        close_index = names.index("close")
        self.assertLess(unsubscribe_index, second_query_index)
        self.assertLess(second_query_index, close_index)

    def test_m1_gate_rejects_premarket_even_with_fresh_quotes(self):
        class PremarketContext(DynamicContext):
            def get_global_state(self):
                self.calls.append(("get_global_state",))
                return 0, {
                    "market_us": "PRE_MARKET_BEGIN",
                    "timestamp": 1788220800,
                }

        context = PremarketContext()
        result = run_probe(
            ["US.TSLA"],
            profile="m1-gate",
            futu_module=FakeFutu(),
            context_factory=lambda: context,
            lock_factory=FakeLock,
        )

        self.assertEqual(result["status"], PARTIAL)
        self.assertEqual(
            result["market_state"]["market_us"], "PRE_MARKET_BEGIN"
        )
        self.assertIn("market_session:not_regular", result["errors"])
        self.assertIn(
            "market_session:not_regular", result["symbols"][0]["errors"]
        )

    def test_lock_failure_does_not_create_context_or_subscribe(self):
        result, context, lock = self._run(lock_acquire=False)
        self.assertEqual(result["status"], "failed")
        self.assertIn("lock", " ".join(result["errors"]))
        self.assertEqual(context.calls, [])
        self.assertFalse(context.closed)
        self.assertTrue(lock.released)

    def test_before_failure_does_not_subscribe_and_releases_resources(self):
        result, context, lock = self._run(
            context_options={"fail_before": True}
        )
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("subscribe", [call[0] for call in context.calls])
        self.assertTrue(context.closed)
        self.assertTrue(lock.released)

    def test_subscribe_failure_is_cleaned_up_conservatively(self):
        result, context, _ = self._run(
            context_options={"fail_subscribe": True}
        )
        self.assertEqual(result["status"], "partial")
        self.assertEqual(
            result["subscription"]["owned_codes"],
            [f"US.TSLA-{DYNAMIC_EXPIRY}-P1"],
        )
        self.assertIn("unsubscribe", [call[0] for call in context.calls])

    def test_existing_code_is_not_subscribed_owned_or_unsubscribed(self):
        result, context, _ = self._run(
            context_options={
                "existing_codes": [f"US.TSLA-{DYNAMIC_EXPIRY}-P1"]
            }
        )
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["subscription"]["owned_codes"], [])
        names = [call[0] for call in context.calls]
        self.assertNotIn("subscribe", names)
        self.assertNotIn("unsubscribe", names)

    def test_cleanup_failure_is_partial_and_still_closes_and_releases(self):
        result, context, lock = self._run(
            context_options={"fail_unsubscribe": True}
        )
        self.assertEqual(result["status"], "partial")
        self.assertEqual(
            result["subscription"]["cleanup_status"], "partial"
        )
        self.assertEqual(
            sum(call[0] == "query_subscription" for call in context.calls), 2
        )
        self.assertTrue(context.closed)
        self.assertTrue(lock.released)

    def test_close_failure_is_partial_and_releases_lock(self):
        result, _, lock = self._run(context_options={"fail_close": True})
        self.assertEqual(result["status"], "partial")
        self.assertIn("close", " ".join(result["errors"]))
        self.assertTrue(lock.released)

    def test_dynamic_fields_include_source_unit_and_independent_time(self):
        result, _, _ = self._run()
        dynamic = result["symbols"][0]["representative_contracts"][0][
            "dynamic_quote"
        ]
        self.assertEqual(dynamic["bid_price"]["raw_field"], "bid_price")
        self.assertEqual(
            dynamic["bid_price"]["unit"],
            "provider_price_unknown_currency",
        )
        self.assertEqual(
            dynamic["bid_price"]["source_method"], "get_market_snapshot"
        )
        self.assertEqual(
            dynamic["bid_price"]["as_of"], "2026-01-01 10:05:00"
        )
        self.assertEqual(dynamic["delta"]["source_method"], "get_stock_quote")
        self.assertEqual(dynamic["delta"]["as_of"], "2026-01-01 10:05:00")
        self.assertEqual(dynamic["delta"]["unit"], "unknown_greek_unit")
        self.assertEqual(dynamic["bid_vol"]["unit"], "provider_volume_unit_unknown")
        self.assertEqual(dynamic["open_interest"]["unit"], "contracts")
        self.assertEqual(
            dynamic["implied_volatility"]["unit"], "percent_points"
        )
        for metadata in dynamic.values():
            for key in (
                "value",
                "raw_field",
                "unit",
                "source_method",
                "as_of",
                "status",
            ):
                self.assertIn(key, metadata)

    def test_missing_critical_field_is_partial(self):
        result, _, _ = self._run(
            context_options={"missing_fields": {"open_interest"}}
        )
        self.assertEqual(result["status"], "partial")
        dynamic = result["symbols"][0]["representative_contracts"][0][
            "dynamic_quote"
        ]
        self.assertEqual(dynamic["open_interest"]["status"], "missing")

    def test_m1_gate_calls_all_optional_capabilities(self):
        result, context, _ = self._run(profile="m1-gate")
        self.assertEqual(result["status"], "partial")
        names = [call[0] for call in context.calls]
        self.assertIn("get_option_exercise_probability", names)
        self.assertIn("get_option_volatility", names)
        self.assertIn("request_history_kline", names)
        self.assertIn("get_earnings_calendar", names)
        self.assertIn("get_dividend_calendar", names)
        self.assertEqual(
            result["symbols"][0]["ex_dividend"]["status"], "ok"
        )
        probability_call = next(
            call
            for call in context.calls
            if call[0] == "get_option_exercise_probability"
        )
        self.assertIsInstance(probability_call[1], str)

    def test_optional_method_errors_are_explicit_partial(self):
        class ErrorOptionalContext(DynamicContext):
            def get_option_exercise_probability(self, code):
                return 1, "not found"

            def get_option_volatility(self, code):
                raise AttributeError("no method")

        context = ErrorOptionalContext()
        lock = FakeLock()
        result = run_probe(
            ["US.TSLA"],
            profile="m1-gate",
            futu_module=FakeFutu(),
            context_factory=lambda: context,
            lock_factory=lambda: lock,
        )
        self.assertEqual(result["status"], "partial")
        analytics = result["symbols"][0]["representative_contracts"][0][
            "analytics"
        ]
        self.assertEqual(analytics["probability"]["status"], "error")
        self.assertEqual(analytics["volatility"]["status"], "error")

    def test_history_three_tuple_uses_data_records(self):
        result, _, _ = self._run(profile="m1-gate")
        history = result["symbols"][0]["history"]
        self.assertEqual(history["status"], "ok")
        self.assertEqual(history["sample_count"], 2)
        self.assertEqual(history["last_date"], "2026-01-03")

    def test_earnings_without_matching_symbol_is_clear(self):
        class NoMatchEarningsContext(DynamicContext):
            def get_earnings_calendar(
                self, market, begin_date=None, end_date=None
            ):
                self.calls.append(("get_earnings_calendar", market))
                return 0, [
                    {"security": "US.MSFT", "earnings_date": "2026-07-20"}
                ]

        context = NoMatchEarningsContext()
        lock = FakeLock()
        result = run_probe(
            ["US.TSLA"],
            profile="m1-gate",
            futu_module=FakeFutu(),
            context_factory=lambda: context,
            lock_factory=lambda: lock,
        )
        earnings = result["symbols"][0]["earnings"]
        self.assertEqual(earnings["status"], "ok")
        self.assertEqual(earnings["event_status"], "clear")

    def test_dynamic_result_is_strict_json_serializable(self):
        result, _, _ = self._run(
            context_options={"fail_subscribe": True}
        )
        json.dumps(result, allow_nan=False)

    def test_maximum_expiration_and_contract_limits(self):
        context = FakeContext()
        result = run_probe(
            ["US.TSLA"],
            max_expirations=2,
            max_contracts_per_expiration=2,
            futu_module=FakeFutu(),
            context_factory=lambda: context,
        )
        symbol_result = result["symbols"][0]
        self.assertEqual(len(symbol_result["expirations"]), 2)
        self.assertEqual(len(symbol_result["representative_contracts"]), 4)
        self.assertEqual(result["status"], "partial")

    def test_subscription_before_failure_is_partial_in_static_mode(self):
        context = FakeContext(fail_before_subscription=True)
        result = run_probe(
            ["US.TSLA"],
            futu_module=FakeFutu(),
            context_factory=lambda: context,
        )
        self.assertEqual(result["status"], "partial")
        self.assertTrue(context.closed)
        self.assertIn("get_option_chain", context.calls)

    def test_context_factory_error_is_sanitized(self):
        def failing_factory():
            raise RuntimeError("connect host=10.0.0.1 port=11111")

        result = run_probe(
            ["US.TSLA"],
            futu_module=FakeFutu(),
            context_factory=failing_factory,
        )
        self.assertEqual(result["status"], "failed")
        error_text = " ".join(result["errors"])
        self.assertNotIn("10.0.0.1", error_text)
        self.assertNotIn("11111", error_text)

    def test_one_symbol_failure_keeps_other_result(self):
        context = FakeContext(fail_expiration={"US.MSFT"})
        result = run_probe(
            ["US.TSLA", "US.MSFT"],
            futu_module=FakeFutu(),
            context_factory=lambda: context,
        )
        self.assertEqual(result["status"], "partial")
        by_symbol = {row["symbol"]: row for row in result["symbols"]}
        self.assertEqual(by_symbol["US.TSLA"]["status"], "partial")
        self.assertEqual(by_symbol["US.MSFT"]["status"], "partial")

    def test_no_expiration_is_partial(self):
        class NoExpirationContext(FakeContext):
            def get_option_expiration_date(self, symbol):
                self.calls.append("get_option_expiration_date")
                return 0, []

        context = NoExpirationContext()
        result = run_probe(
            ["US.TSLA"],
            futu_module=FakeFutu(),
            context_factory=lambda: context,
        )
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["symbols"][0]["expirations"], [])

    def test_no_standard_put_is_partial(self):
        context = FakeContext(no_standard_put={"US.TSLA"})
        result = run_probe(
            ["US.TSLA"],
            futu_module=FakeFutu(),
            context_factory=lambda: context,
        )
        self.assertEqual(result["status"], "partial")
        self.assertEqual(
            result["symbols"][0]["representative_contracts"], []
        )

    def test_capability_matrix_marks_unimplemented_methods_unsupported(self):
        context = FakeContext()
        result = run_probe(
            ["US.TSLA"],
            futu_module=FakeFutu(),
            context_factory=lambda: context,
        )
        self.assertEqual(
            result["capabilities"]["subscribe"]["status"], "unsupported"
        )
        self.assertIsNone(
            result["capabilities"]["subscribe"]["signature"]
        )
        self.assertEqual(
            result["capabilities"]["get_earnings_calendar"]["status"],
            "unsupported",
        )
        query_capability = result["capabilities"]["query_subscription"]
        self.assertEqual(query_capability["status"], "supported")
        self.assertIn("is_all_conn", query_capability["signature"])

    def test_invalid_limit_does_not_create_context(self):
        context = FakeContext()
        result = run_probe(
            ["US.TSLA"],
            max_expirations=5,
            futu_module=FakeFutu(),
            context_factory=lambda: context,
        )
        self.assertEqual(result["status"], "failed")
        self.assertFalse(context.closed)

    def test_invalid_symbol_does_not_create_context(self):
        context = FakeContext()
        result = run_probe(
            ["us.bad"],
            futu_module=FakeFutu(),
            context_factory=lambda: context,
        )
        self.assertEqual(result["status"], "failed")
        self.assertFalse(context.closed)

    def test_m1_gate_allow_partial_does_not_create_context(self):
        context = FakeContext()
        result = run_probe(
            ["US.TSLA"],
            profile="m1-gate",
            allow_partial=True,
            futu_module=FakeFutu(),
            context_factory=lambda: context,
        )
        self.assertEqual(result["status"], "failed")
        self.assertFalse(context.closed)

    def test_symbol_exception_isolated_and_context_closed(self):
        context = FakeContext(
            symbol_exception={"US.TSLA": RuntimeError("boom")}
        )
        result = run_probe(
            ["US.TSLA", "US.MSFT"],
            futu_module=FakeFutu(),
            context_factory=lambda: context,
        )
        self.assertTrue(context.closed)
        self.assertEqual(len(result["symbols"]), 2)
        self.assertEqual(
            {row["symbol"] for row in result["symbols"]},
            {"US.TSLA", "US.MSFT"},
        )

    def test_subscription_after_failure_is_partial_and_closes(self):
        context = FakeContext(fail_after_subscription=True)
        result = run_probe(
            ["US.TSLA"],
            futu_module=FakeFutu(),
            context_factory=lambda: context,
        )
        self.assertEqual(result["status"], "partial")
        self.assertTrue(context.closed)

    def test_static_result_is_strict_json_serializable(self):
        context = FakeContext()
        result = run_probe(
            ["US.TSLA", "US.MSFT"],
            futu_module=FakeFutu(),
            context_factory=lambda: context,
        )
        json.dumps(result, allow_nan=False)


class ProbeLockContentionTest(SimpleTestCase):
    def test_second_instance_waits_until_first_releases(self):
        if sys.platform != "win32" and os.name != "posix":
            self.skipTest("file locking is unsupported on this platform")
        lock_name = "test_futu_option_probe_contention.lock"
        first = ProbeLock(name=lock_name)
        second = ProbeLock(name=lock_name)
        self.assertTrue(first.acquire())
        try:
            self.assertFalse(second.acquire())
        finally:
            first.release()
        self.assertTrue(second.acquire())
        second.release()


@patch("portfolio.futu_option_probe.MIN_SUBSCRIPTION_SECONDS", 0)
class ProbeFlowSafetyTest(SimpleTestCase):
    def _run_dynamic(self, context=None, profile="static", **options):
        context = context or DynamicContext()
        lock = FakeLock()
        result = run_probe(
            ["US.TSLA"],
            profile=profile,
            subscribe_quotes=True,
            futu_module=FakeFutu(),
            context_factory=lambda: context,
            lock_factory=lambda: lock,
            **options,
        )
        return result, context, lock

    def test_dte_uses_new_york_calendar_date(self):
        class ExpirationContext(DynamicContext):
            def get_option_expiration_date(self, symbol):
                return 0, [
                    {"strike_time": "2026-07-15"},
                    {"strike_time": "2026-07-19"},
                ]

        config = resolve_profile(
            "static", False, False, False, False, False
        )
        result = probe_symbol(
            ExpirationContext(),
            FakeFutu(),
            "US.TSLA",
            config,
            2,
            1,
            set(),
            [],
            probe_dt=datetime(
                2026, 7, 10, 2, 0, 0, tzinfo=timezone.utc
            ),
        )
        first, second = result["expirations"]
        self.assertEqual(first["dte"], 6)
        self.assertEqual(first["weekday"], "Wednesday")
        self.assertFalse(first["is_7_to_30_dte"])
        self.assertEqual(second["dte"], 10)
        self.assertTrue(second["is_7_to_30_dte"])

    def test_invalid_expiration_is_visible_and_partial(self):
        class InvalidExpirationContext(DynamicContext):
            def get_option_expiration_date(self, symbol):
                return 0, [{"strike_time": "not-a-date"}]

        config = resolve_profile(
            "static", False, False, False, False, False
        )
        result = probe_symbol(
            InvalidExpirationContext(),
            FakeFutu(),
            "US.TSLA",
            config,
            1,
            1,
            set(),
            [],
            probe_dt=datetime(2026, 7, 10, tzinfo=timezone.utc),
        )
        rejected = result["rejected_expirations"]
        self.assertEqual(rejected[0]["status"], "parse_error")
        self.assertIsNone(rejected[0]["dte"])
        self.assertEqual(result["status"], PARTIAL)

    def test_contract_identity_is_raw_and_fail_closed(self):
        result, _, _ = self._run_dynamic()
        contract = result["symbols"][0]["representative_contracts"][0]
        for field in (
            "code",
            "option_type",
            "stock_owner",
            "strike_time",
            "expiration_date",
            "strike_price",
            "lot_size",
            "option_standard_type",
            "option_settlement_mode",
            "index_option_type",
        ):
            self.assertIn(field, contract)
        self.assertIsNone(contract["deliverable_shares"])
        self.assertIsNone(contract["exercise_style"])
        self.assertEqual(contract["contract_identity_status"], "partial")
        self.assertNotIn("deliverable_multiplier", contract)

    def test_consistent_provider_contract_identity_is_verified(self):
        class ConsistentIdentityContext(DynamicContext):
            def get_market_snapshot(self, codes):
                ret, rows = super().get_market_snapshot(codes)
                if codes[0] not in ("US.TSLA", "US.MSFT"):
                    rows[0]["option_contract_size"] = 100
                    rows[0]["option_contract_multiplier"] = 100
                    rows[0]["option_area_type"] = "AMERICAN"
                return ret, rows

            def get_stock_quote(self, codes):
                ret, rows = super().get_stock_quote(codes)
                rows[0]["contract_size"] = 100
                return ret, rows

        result, _, _ = self._run_dynamic(ConsistentIdentityContext())
        contract = result["symbols"][0]["representative_contracts"][0]
        self.assertEqual(contract["contract_identity_status"], "ok")
        self.assertEqual(contract["deliverable_shares"], 100)
        self.assertEqual(contract["exercise_style"], "AMERICAN")
        self.assertEqual(contract["identity_unknown_fields"], [])

    def test_unknown_settlement_keeps_contract_identity_partial(self):
        class UnknownSettlementContext(DynamicContext):
            def get_option_chain(
                self, symbol, start=None, end=None, option_type=None
            ):
                ret, rows = super().get_option_chain(
                    symbol,
                    start=start,
                    end=end,
                    option_type=option_type,
                )
                rows[0]["option_settlement_mode"] = "N/A"
                return ret, rows

            def get_market_snapshot(self, codes):
                ret, rows = super().get_market_snapshot(codes)
                if codes[0] not in ("US.TSLA", "US.MSFT"):
                    rows[0]["option_contract_size"] = 100
                    rows[0]["option_area_type"] = "AMERICAN"
                return ret, rows

            def get_stock_quote(self, codes):
                ret, rows = super().get_stock_quote(codes)
                rows[0]["contract_size"] = 100
                return ret, rows

        result, _, _ = self._run_dynamic(UnknownSettlementContext())
        contract = result["symbols"][0]["representative_contracts"][0]
        self.assertEqual(contract["contract_identity_status"], "partial")
        self.assertEqual(contract["deliverable_shares"], 100)
        self.assertEqual(contract["exercise_style"], "AMERICAN")
        self.assertEqual(
            contract["identity_unknown_fields"],
            ["option_settlement_mode"],
        )

    def test_non_integer_deliverable_size_is_rejected(self):
        class FractionalIdentityContext(DynamicContext):
            def get_market_snapshot(self, codes):
                ret, rows = super().get_market_snapshot(codes)
                if codes[0] not in ("US.TSLA", "US.MSFT"):
                    rows[0]["option_contract_size"] = 100.5
                    rows[0]["option_area_type"] = "AMERICAN"
                return ret, rows

            def get_stock_quote(self, codes):
                ret, rows = super().get_stock_quote(codes)
                rows[0]["contract_size"] = 100.5
                return ret, rows

            def get_option_chain(
                self, symbol, start=None, end=None, option_type=None
            ):
                ret, rows = super().get_option_chain(
                    symbol,
                    start=start,
                    end=end,
                    option_type=option_type,
                )
                rows[0]["lot_size"] = 100.5
                return ret, rows

        result, _, _ = self._run_dynamic(FractionalIdentityContext())
        contract = result["symbols"][0]["representative_contracts"][0]
        self.assertEqual(contract["contract_identity_status"], "partial")
        self.assertIsNone(contract["deliverable_shares"])
        self.assertIn(
            "deliverable_shares", contract["identity_unknown_fields"]
        )

    def test_dynamic_contract_fields_come_from_snapshot(self):
        result, _, _ = self._run_dynamic()
        dynamic = result["symbols"][0]["representative_contracts"][0][
            "dynamic_quote"
        ]
        expected = {
            "option_contract_size": 25,
            "option_owner_lot_multiplier": 2,
            "option_contract_multiplier": 50,
            "option_area_type": "US",
        }
        for field, value in expected.items():
            self.assertEqual(dynamic[field]["value"], value)
            self.assertEqual(
                dynamic[field]["source_method"], "get_market_snapshot"
            )
        self.assertNotEqual(
            dynamic["option_contract_multiplier"]["value"],
            result["symbols"][0]["representative_contracts"][0][
                "lot_size"
            ],
        )

    def test_dynamic_delay_and_freshness_are_derived_from_provider_time(self):
        result, _, _ = self._run_dynamic()
        dynamic = result["symbols"][0]["representative_contracts"][0][
            "dynamic_quote"
        ]
        expected = {
            "snapshot_delay_status": "delayed",
            "snapshot_freshness_status": "stale",
            "quote_delay_status": "delayed",
            "quote_freshness_status": "stale",
        }
        for field, value in expected.items():
            self.assertEqual(dynamic[field]["value"], value)
            self.assertEqual(dynamic[field]["status"], "ok")
            self.assertEqual(
                set(dynamic[field]),
                {
                    "value",
                    "raw_field",
                    "unit",
                    "source_method",
                    "as_of",
                    "status",
                },
            )

    def test_empty_analytics_is_not_success(self):
        class EmptyAnalyticsContext(DynamicContext):
            def get_option_exercise_probability(self, code):
                return 0, []

        result, _, _ = self._run_dynamic(
            EmptyAnalyticsContext(), profile="m1-gate"
        )
        probability = result["symbols"][0]["representative_contracts"][0][
            "analytics"
        ]["probability"]
        self.assertEqual(probability["status"], "empty")
        self.assertEqual(probability["fields"], {})
        self.assertEqual(result["status"], PARTIAL)

    def test_analytics_fields_have_provenance_metadata(self):
        result, _, _ = self._run_dynamic(profile="m1-gate")
        probability = result["symbols"][0]["representative_contracts"][0][
            "analytics"
        ]["probability"]
        self.assertNotIn("records", probability)
        for raw_field, metadata in probability["fields"].items():
            self.assertEqual(metadata["raw_field"], raw_field)
            self.assertNotEqual(metadata["unit"], "provider_native")
            self.assertEqual(
                metadata["source_method"],
                "get_option_exercise_probability",
            )
            self.assertIn("as_of", metadata)
            self.assertIn("status", metadata)
        self.assertEqual(
            probability["fields"]["strike_probability"]["unit"],
            "percent_points",
        )
        self.assertEqual(
            probability["fields"]["timestamp"]["unit"],
            "unix_seconds",
        )

    def test_analytics_time_without_required_metric_is_partial(self):
        class MissingProbabilityContext(DynamicContext):
            def get_option_exercise_probability(self, code):
                return 0, [
                    {
                        "timestamp": 1788220800,
                        "timestamp_str": "2026-09-01",
                    }
                ]

        result, _, _ = self._run_dynamic(
            MissingProbabilityContext(), profile="m1-gate"
        )
        probability = result["symbols"][0]["representative_contracts"][0][
            "analytics"
        ]["probability"]
        self.assertEqual(probability["status"], "partial")
        self.assertEqual(
            probability["missing_required_fields"],
            ["strike_probability"],
        )

    def test_analytics_nan_required_metric_is_missing(self):
        class NanVolatilityContext(DynamicContext):
            def get_option_volatility(self, code):
                return 0, [
                    {
                        "timestamp": 1788220800,
                        "timestamp_str": "2026-09-01",
                        "implied_volatility": 25.0,
                        "history_volatility": float("nan"),
                        "volatility_premium": 3.0,
                    }
                ]

        result, _, _ = self._run_dynamic(
            NanVolatilityContext(), profile="m1-gate"
        )
        volatility = result["symbols"][0]["representative_contracts"][0][
            "analytics"
        ]["volatility"]
        self.assertEqual(volatility["status"], "partial")
        self.assertIn(
            "history_volatility",
            volatility["missing_required_fields"],
        )
        self.assertEqual(
            volatility["fields"]["history_volatility"]["status"],
            "missing",
        )

    def test_invalid_analytics_timestamps_are_unknown(self):
        class InvalidAnalyticsTimeContext(DynamicContext):
            def __init__(self, invalid_timestamp):
                super().__init__()
                self.invalid_timestamp = invalid_timestamp

            def get_option_exercise_probability(self, code):
                return 0, [
                    {
                        "timestamp": self.invalid_timestamp,
                        "timestamp_str": "not-a-date",
                        "security_price": 200.0,
                        "strike_probability": 35.0,
                    }
                ]

        invalid_values = (
            Decimal("NaN"),
            -1,
            "123",
            Decimal("1E+100"),
        )
        for value in invalid_values:
            with self.subTest(value=value):
                result, _, _ = self._run_dynamic(
                    InvalidAnalyticsTimeContext(value), profile="m1-gate"
                )
                probability = result["symbols"][0][
                    "representative_contracts"
                ][0]["analytics"]["probability"]
                self.assertEqual(probability["status"], "partial")
                self.assertEqual(probability["as_of_status"], "unknown")
                self.assertIsNone(
                    probability["fields"]["timestamp"]["value"]
                )
                self.assertEqual(
                    probability["fields"]["timestamp"]["status"],
                    "missing",
                )
                self.assertIsNone(
                    probability["fields"]["timestamp_str"]["value"]
                )
                self.assertEqual(
                    probability["fields"]["timestamp_str"]["status"],
                    "missing",
                )

    def test_earnings_security_match_and_query_window(self):
        result, _, _ = self._run_dynamic(profile="m1-gate")
        earnings = result["symbols"][0]["earnings"]
        self.assertEqual(earnings["status"], "ok")
        self.assertEqual(earnings["records"][0]["security"], "US.TSLA")
        self.assertEqual(
            set(earnings["query_window"]), {"begin", "end"}
        )
        begin = datetime.fromisoformat(earnings["query_window"]["begin"])
        end = datetime.fromisoformat(earnings["query_window"]["end"])
        self.assertEqual((end - begin).days, 6)

    def test_earnings_code_fallback(self):
        class CodeEarningsContext(DynamicContext):
            def get_earnings_calendar(
                self, market, begin_date=None, end_date=None
            ):
                return 0, [{"code": "US.TSLA", "earnings_date": end_date}]

        result, _, _ = self._run_dynamic(
            CodeEarningsContext(), profile="m1-gate"
        )
        earnings = result["symbols"][0]["earnings"]
        self.assertEqual(earnings["status"], "ok")
        self.assertEqual(earnings["records"][0]["code"], "US.TSLA")

    def test_event_calendars_are_shared_across_symbols(self):
        context = DynamicContext()
        result = run_probe(
            ["US.TSLA", "US.MSFT"],
            profile="m1-gate",
            max_expirations=1,
            max_contracts_per_expiration=1,
            futu_module=FakeFutu(),
            context_factory=lambda: context,
            lock_factory=FakeLock,
        )
        earnings_calls = [
            call
            for call in context.calls
            if call[0] == "get_earnings_calendar"
        ]
        dividend_calls = [
            call
            for call in context.calls
            if call[0] == "get_dividend_calendar"
        ]
        self.assertEqual(len(earnings_calls), 1)
        self.assertEqual(len(dividend_calls), 7)
        self.assertEqual(
            result["symbols"][0]["ex_dividend"]["status"], "ok"
        )
        self.assertEqual(
            result["symbols"][1]["ex_dividend"]["status"], "ok"
        )
        self.assertEqual(result["symbols"][1]["ex_dividend"]["event_status"], "clear")

    def test_incomplete_dividend_calendar_is_partial(self):
        class IncompleteDividendContext(DynamicContext):
            def get_dividend_calendar(
                self, market, date, data_from=None, count=None
            ):
                if data_from:
                    return 0, (2, [])
                return 0, (
                    2,
                    [{"security": "US.TSLA", "ex_date": date}],
                )

        result, _, _ = self._run_dynamic(
            IncompleteDividendContext(), profile="m1-gate"
        )
        ex_dividend = result["symbols"][0]["ex_dividend"]
        self.assertEqual(ex_dividend["status"], "partial")
        self.assertEqual(
            ex_dividend["issues"][0]["category"],
            "incomplete_pagination",
        )

    def test_dividend_calendar_paginates_to_provider_total(self):
        class PaginatedDividendContext(DynamicContext):
            def __init__(self):
                super().__init__()
                self.first_dividend_date = None

            def get_dividend_calendar(
                self, market, date, data_from=None, count=None
            ):
                self.calls.append(
                    ("get_dividend_calendar", market, date, data_from, count)
                )
                if self.first_dividend_date is None:
                    self.first_dividend_date = date
                if date != self.first_dividend_date:
                    return 0, (0, [])
                if data_from == 0:
                    return 0, (
                        201,
                        [
                            {"security": f"US.OTHER{index}", "ex_date": date}
                            for index in range(200)
                        ],
                    )
                return 0, (
                    201,
                    [{"security": "US.TSLA", "ex_date": date}],
                )

        context = PaginatedDividendContext()
        result, _, _ = self._run_dynamic(context, profile="m1-gate")
        ex_dividend = result["symbols"][0]["ex_dividend"]
        calls = [
            call
            for call in context.calls
            if call[0] == "get_dividend_calendar"
        ]
        self.assertEqual(ex_dividend["status"], "ok")
        self.assertEqual(ex_dividend["records"][0]["security"], "US.TSLA")
        self.assertEqual(len(calls), 8)
        self.assertEqual(calls[0][3:], (0, 200))
        self.assertEqual(calls[1][3:], (200, 200))

    def test_earnings_no_match_and_ex_dividend_are_clear(self):
        class NoMatchEarningsContext(DynamicContext):
            def get_earnings_calendar(
                self, market, begin_date=None, end_date=None
            ):
                return 0, [{"security": "US.MSFT"}]

            def get_dividend_calendar(
                self, market, date, data_from=None, count=None
            ):
                return 0, (
                    1,
                    [{"security": "US.MSFT", "ex_date": date}],
                )

        result, _, _ = self._run_dynamic(
            NoMatchEarningsContext(), profile="m1-gate"
        )
        symbol = result["symbols"][0]
        self.assertEqual(symbol["earnings"]["status"], "ok")
        self.assertEqual(symbol["earnings"]["event_status"], "clear")
        self.assertEqual(symbol["ex_dividend"]["status"], "ok")
        self.assertEqual(symbol["ex_dividend"]["event_status"], "clear")

    def test_each_missing_dynamic_method_fails_before_subscribe(self):
        class MissingMethodContext(DynamicContext):
            def __init__(self, missing_method):
                super().__init__()
                self.missing_method = missing_method

            def __getattribute__(self, name):
                try:
                    missing = object.__getattribute__(
                        self, "missing_method"
                    )
                except AttributeError:
                    missing = None
                if name == missing:
                    raise AttributeError(name)
                return super().__getattribute__(name)

        required = (
            "query_subscription",
            "subscribe",
            "unsubscribe",
            "get_market_snapshot",
            "get_stock_quote",
        )
        for method in required:
            with self.subTest(method=method):
                context = MissingMethodContext(method)
                result, context, lock = self._run_dynamic(context)
                names = [
                    call[0] if isinstance(call, tuple) else call
                    for call in context.calls
                ]
                self.assertEqual(result["status"], FAILED)
                self.assertNotIn("subscribe", names)
                self.assertTrue(context.closed)
                self.assertTrue(lock.released)

    def test_requested_optional_method_unsupported_is_explicit(self):
        class MissingMethodContext(DynamicContext):
            def __init__(self, missing_method):
                super().__init__()
                self.missing_method = missing_method

            def __getattribute__(self, name):
                if name == object.__getattribute__(self, "missing_method"):
                    raise AttributeError(name)
                return super().__getattribute__(name)

        cases = (
            ("get_option_exercise_probability", {"include_option_analytics": True}),
            ("get_option_volatility", {"include_option_analytics": True}),
            ("request_history_kline", {"include_history": True}),
            ("get_earnings_calendar", {"include_earnings": True}),
            ("get_dividend_calendar", {"include_earnings": True}),
        )
        for method, option in cases:
            with self.subTest(method=method):
                context = MissingMethodContext(method)
                result = run_probe(
                    ["US.TSLA"],
                    futu_module=FakeFutu(),
                    context_factory=lambda: context,
                    **option,
                )
                self.assertEqual(result["status"], PARTIAL)
                self.assertIn(
                    f"optional_method_unsupported:{method}",
                    result["errors"],
                )
                self.assertTrue(context.closed)

    def _run_after_state(
        self,
        after_codes=None,
        after_own_used=5,
        after_own_option_quota=1,
        after_quota_overrides=None,
        after_mode="ok",
    ):
        class AfterStateContext(DynamicContext):
            def query_subscription(inner_self, is_all_conn=False):
                inner_self.calls.append(("query_subscription", is_all_conn))
                inner_self.subscription_calls += 1
                if inner_self.subscription_calls == 1:
                    return 0, {
                        **SubscriptionSummaryTest.FULL_ROW,
                        "own_used": 5,
                        "own_option_used_quota": 1,
                        "sub_list": {"QUOTE": []},
                    }
                if after_mode == "provider_error":
                    return 1, "after unavailable"
                if after_mode == "malformed":
                    return 0, {"sub_list": {"QUOTE": []}}
                row = {
                    **SubscriptionSummaryTest.FULL_ROW,
                    "own_used": after_own_used,
                    "own_option_used_quota": after_own_option_quota,
                    "sub_list": {"QUOTE": list(after_codes or [])},
                }
                row.update(after_quota_overrides or {})
                return 0, row

        return self._run_dynamic(AfterStateContext())

    def test_residual_candidate_fails_cleanup_verification(self):
        code = f"US.TSLA-{DYNAMIC_EXPIRY}-P1"
        result, context, lock = self._run_after_state(after_codes=[code])
        verification = result["subscription"]["verification"]
        self.assertEqual(result["subscription"]["cleanup_status"], "failed")
        self.assertFalse(verification["checks"]["quote_codes_match"])
        self.assertFalse(
            verification["checks"]["cleanup_candidates_absent"]
        )
        self.assertTrue(context.closed)
        self.assertTrue(lock.released)

    def test_any_subscription_quota_difference_fails_verification(self):
        quota_fields = (
            "total_used",
            "remain",
            "own_used",
            "option_used_quota",
            "option_remain_quota",
            "own_option_used_quota",
        )
        for field in quota_fields:
            with self.subTest(field=field):
                original = SubscriptionSummaryTest.FULL_ROW[field]
                result, context, lock = self._run_after_state(
                    after_quota_overrides={field: original + 1}
                )
                self.assertEqual(
                    result["subscription"]["cleanup_status"], "failed"
                )
                self.assertFalse(
                    result["subscription"]["verification"]["checks"][
                        f"{field}_match"
                    ]
                )
                self.assertTrue(context.closed)
                self.assertTrue(lock.released)

    def test_unavailable_after_state_never_claims_restored(self):
        for mode in ("provider_error", "malformed"):
            with self.subTest(mode=mode):
                result, context, lock = self._run_after_state(
                    after_mode=mode
                )
                self.assertNotEqual(
                    result["subscription"]["cleanup_status"], "restored"
                )
                self.assertTrue(context.closed)
                self.assertTrue(lock.released)

    def test_subscription_schema_is_nested_and_auditable(self):
        result, _, _ = self._run_dynamic()
        self.assertNotIn("owned_codes", result)
        self.assertNotIn("cleanup_status", result)
        subscription = result["subscription"]
        for key in (
            "owned_codes",
            "cleanup_status",
            "existing_quote_codes_before",
            "existing_quote_codes_after",
            "verification",
        ):
            self.assertIn(key, subscription)


@patch("portfolio.futu_option_probe.MIN_SUBSCRIPTION_SECONDS", 0)
class FinalFlowGuardTest(SimpleTestCase):
    @staticmethod
    def _symbols(count):
        return [f"US.SYM{index:02d}" for index in range(count)]

    def test_symbol_limit_fails_before_context_creation(self):
        created = []

        result = run_probe(
            self._symbols(21),
            futu_module=FakeFutu(),
            context_factory=lambda: created.append(True),
        )

        self.assertEqual(result["status"], FAILED)
        self.assertIn("MAX_SYMBOLS=20", result["errors"][0])
        self.assertEqual(created, [])

    def test_dynamic_candidate_limit_fails_before_side_effects(self):
        for profile in ("static", "m1-gate"):
            with self.subTest(profile=profile):
                contexts = []
                locks = []
                result = run_probe(
                    self._symbols(10),
                    profile=profile,
                    subscribe_quotes=profile == "static",
                    max_expirations=3,
                    max_contracts_per_expiration=1,
                    futu_module=FakeFutu(),
                    context_factory=lambda: contexts.append(True),
                    lock_factory=lambda: locks.append(True),
                )
                self.assertEqual(result["status"], FAILED)
                self.assertIn(
                    "dynamic candidate limit exceeded",
                    result["errors"][0],
                )
                self.assertEqual(contexts, [])
                self.assertEqual(locks, [])

    def test_three_symbol_live_batch_fits_dynamic_candidate_limit(self):
        context = DynamicContext()
        lock = FakeLock()
        result = run_probe(
            ["US.TSLA", "US.MSFT", "US.NVDA"],
            profile="m1-gate",
            max_expirations=3,
            max_contracts_per_expiration=3,
            futu_module=FakeFutu(),
            context_factory=lambda: context,
            lock_factory=lambda: lock,
        )

        self.assertNotIn("dynamic candidate limit exceeded", result["errors"])
        self.assertTrue(context.calls)
        self.assertTrue(context.closed)
        self.assertTrue(lock.released)

    def test_static_probe_is_not_subject_to_dynamic_candidate_limit(self):
        context = FakeContext()
        result = run_probe(
            self._symbols(13),
            max_expirations=1,
            max_contracts_per_expiration=1,
            futu_module=FakeFutu(),
            context_factory=lambda: context,
        )
        self.assertEqual(result["status"], PARTIAL)
        self.assertEqual(len(result["symbols"]), 13)
        self.assertTrue(context.closed)

    def test_expired_and_unparseable_expirations_are_not_probed(self):
        class MixedExpirationContext(DynamicContext):
            def get_option_expiration_date(self, symbol):
                return 0, [
                    {"strike_time": "2026-08-01"},
                    {"strike_time": "not-a-date"},
                    {"strike_time": "2026-09-04"},
                ]

        context = MixedExpirationContext()
        config = resolve_profile(
            "static", False, False, False, False, False
        )
        result = probe_symbol(
            context,
            FakeFutu(),
            "US.TSLA",
            config,
            3,
            1,
            set(),
            [],
            probe_dt=datetime(2026, 9, 1, tzinfo=timezone.utc),
        )

        chain_calls = [
            call for call in context.calls if call[0] == "get_option_chain"
        ]
        self.assertEqual(len(chain_calls), 1)
        self.assertEqual(result["expirations"][0]["strike_time"], "2026-09-04")
        self.assertEqual(
            {item["status"] for item in result["rejected_expirations"]},
            {"expired", "parse_error"},
        )
        self.assertFalse(
            any(
                isinstance(error, dict)
                and error.get("status") == "expired"
                for error in result["errors"]
            )
        )
        code = result["representative_contracts"][0]["code"]
        self.assertIn("2026-09-04", code)

    def test_only_expired_expirations_never_fetch_a_chain(self):
        class ExpiredOnlyContext(DynamicContext):
            def get_option_expiration_date(self, symbol):
                return 0, [{"strike_time": "2026-08-01"}]

        context = ExpiredOnlyContext()
        config = resolve_profile(
            "static", False, False, False, False, False
        )
        result = probe_symbol(
            context,
            FakeFutu(),
            "US.TSLA",
            config,
            1,
            1,
            set(),
            [],
            probe_dt=datetime(2026, 9, 1, tzinfo=timezone.utc),
        )

        self.assertEqual(result["status"], PARTIAL)
        self.assertEqual(result["expirations"], [])
        self.assertNotIn(
            "get_option_chain", [call[0] for call in context.calls]
        )

    def test_subscribe_exception_still_cleans_registered_candidate(self):
        class RaisingSubscribeContext(DynamicContext):
            def subscribe(self, codes, subtypes, **kwargs):
                code = codes[0]
                self.calls.append(("subscribe", tuple(codes), tuple(subtypes)))
                self.existing_codes.add(code)
                raise RuntimeError("subscribe outcome unknown")

            def unsubscribe(self, codes, subtypes, unsubscribe_all=False):
                self.calls.append(
                    (
                        "unsubscribe",
                        tuple(codes),
                        tuple(subtypes),
                        unsubscribe_all,
                    )
                )
                self.existing_codes.difference_update(codes)
                return 0, None

        context = RaisingSubscribeContext()
        lock = FakeLock()
        result = run_probe(
            ["US.TSLA"],
            subscribe_quotes=True,
            futu_module=FakeFutu(),
            context_factory=lambda: context,
            lock_factory=lambda: lock,
        )

        candidate = f"US.TSLA-{DYNAMIC_EXPIRY}-P1"
        subscription = result["subscription"]
        self.assertIn(candidate, subscription["owned_codes"])
        self.assertEqual(subscription["cleanup_call_status"], "restored")
        self.assertEqual(subscription["cleanup_status"], "restored")
        self.assertEqual(subscription["verification"]["status"], "restored")
        self.assertEqual(context.existing_codes, set())
        self.assertTrue(context.closed)
        self.assertTrue(lock.released)
    def test_duplicate_candidate_is_subscribed_and_cleaned_once(self):
        shared_code = "US.SHARED-2026-09-04-P1"

        class SamePutContext(DynamicContext):
            def get_market_snapshot(self, codes):
                if codes[0] in ("US.TSLA", "US.MSFT"):
                    self.calls.append(("get_market_snapshot", tuple(codes)))
                    return 0, [
                        {
                            "last_price": 200.0,
                            "update_time": "2026-01-01 10:00:00",
                            "sec_status": "NORMAL",
                            "suspension": False,
                        }
                    ]
                return super().get_market_snapshot(codes)

            def get_option_chain(
                self, symbol, start=None, end=None, option_type=None
            ):
                self.calls.append(("get_option_chain", symbol))
                return 0, [
                    {
                        "code": shared_code,
                        "option_type": "PUT",
                        "strike_price": 190,
                        "option_standard_type": "STANDARD",
                        "strike_time": start,
                        "expiration_date": start,
                        "lot_size": 100,
                        "stock_owner": symbol,
                    }
                ]

        context = SamePutContext()
        result = run_probe(
            ["US.TSLA", "US.MSFT"],
            subscribe_quotes=True,
            max_expirations=1,
            max_contracts_per_expiration=1,
            futu_module=FakeFutu(),
            context_factory=lambda: context,
            lock_factory=FakeLock,
        )
        subscribe_calls = [
            call for call in context.calls if call[0] == "subscribe"
        ]
        unsubscribe_calls = [
            call for call in context.calls if call[0] == "unsubscribe"
        ]
        self.assertEqual(len(subscribe_calls), 1)
        self.assertEqual(len(unsubscribe_calls), 1)
        self.assertEqual(result["subscription"]["owned_codes"], [shared_code])

    def test_subscription_queries_are_current_connection_only(self):
        context = DynamicContext()
        result = run_probe(
            ["US.TSLA"],
            subscribe_quotes=True,
            futu_module=FakeFutu(),
            context_factory=lambda: context,
            lock_factory=FakeLock,
        )
        query_calls = [
            call for call in context.calls if call[0] == "query_subscription"
        ]
        self.assertNotEqual(result["status"], FAILED)
        self.assertTrue(query_calls)
        self.assertTrue(all(call[1] is False for call in query_calls))

    def test_malformed_subscription_list_fails_closed(self):
        class MalformedSubscriptionContext(DynamicContext):
            def query_subscription(self, is_all_conn=False):
                self.calls.append(("query_subscription", is_all_conn))
                return 0, {
                    **SubscriptionSummaryTest.FULL_ROW,
                    "sub_list": {"QUOTE": 42},
                }

        context = MalformedSubscriptionContext()
        lock = FakeLock()
        result = run_probe(
            ["US.TSLA"],
            subscribe_quotes=True,
            futu_module=FakeFutu(),
            context_factory=lambda: context,
            lock_factory=lambda: lock,
        )

        self.assertEqual(result["status"], FAILED)
        self.assertNotIn(
            "subscribe", [call[0] for call in context.calls]
        )
        self.assertTrue(context.closed)
        self.assertTrue(lock.released)

    def test_invalid_subscription_quota_fails_closed(self):
        class InvalidQuotaContext(DynamicContext):
            def query_subscription(self, is_all_conn=False):
                self.calls.append(("query_subscription", is_all_conn))
                return 0, {
                    **SubscriptionSummaryTest.FULL_ROW,
                    "total_used": -1,
                    "sub_list": {"QUOTE": []},
                }

        context = InvalidQuotaContext()
        lock = FakeLock()
        result = run_probe(
            ["US.TSLA"],
            subscribe_quotes=True,
            futu_module=FakeFutu(),
            context_factory=lambda: context,
            lock_factory=lambda: lock,
        )

        self.assertEqual(result["status"], FAILED)
        self.assertNotIn(
            "subscribe", [call[0] for call in context.calls]
        )
        self.assertTrue(context.closed)
        self.assertTrue(lock.released)


class SubscriptionMinimumDurationTest(SimpleTestCase):
    def test_cleanup_waits_for_provider_minimum_without_pushes(self):
        class FakeClock:
            def __init__(self):
                self.now = 100.0
                self.sleeps = []

            def monotonic(self):
                return self.now

            def sleep(self, seconds):
                self.sleeps.append(seconds)
                self.now += seconds

        clock = FakeClock()

        class TimedContext(DynamicContext):
            def subscribe(self, codes, subtypes, **kwargs):
                self.subscribe_at = clock.monotonic()
                self.subscribe_kwargs = kwargs
                return super().subscribe(codes, subtypes, **kwargs)

            def unsubscribe(
                self, codes, subtypes, unsubscribe_all=False
            ):
                self.unsubscribe_at = clock.monotonic()
                return super().unsubscribe(
                    codes, subtypes, unsubscribe_all=unsubscribe_all
                )

        context = TimedContext()
        result = run_probe(
            ["US.TSLA"],
            subscribe_quotes=True,
            futu_module=FakeFutu(),
            context_factory=lambda: context,
            lock_factory=FakeLock,
            monotonic=clock.monotonic,
            sleeper=clock.sleep,
        )

        self.assertGreaterEqual(
            context.unsubscribe_at - context.subscribe_at,
            61.0,
        )
        self.assertTrue(clock.sleeps)
        self.assertLessEqual(max(clock.sleeps), 5.0)
        self.assertFalse(context.subscribe_kwargs["subscribe_push"])
        self.assertEqual(
            result["subscription"]["cleanup_wait_seconds"], 61.0
        )
        self.assertEqual(
            result["subscription"]["cleanup_status"], "restored"
        )

    def test_minimum_duration_starts_after_slow_subscribe_returns(self):
        class FakeClock:
            def __init__(self):
                self.now = 200.0

            def monotonic(self):
                return self.now

            def sleep(self, seconds):
                self.now += seconds

        clock = FakeClock()

        class SlowSubscribeContext(DynamicContext):
            def subscribe(self, codes, subtypes, **kwargs):
                self.subscribe_started_at = clock.monotonic()
                clock.now += 10.0
                result = super().subscribe(codes, subtypes, **kwargs)
                self.subscribe_returned_at = clock.monotonic()
                return result

            def unsubscribe(
                self, codes, subtypes, unsubscribe_all=False
            ):
                self.unsubscribe_at = clock.monotonic()
                return super().unsubscribe(
                    codes, subtypes, unsubscribe_all=unsubscribe_all
                )

        context = SlowSubscribeContext()
        result = run_probe(
            ["US.TSLA"],
            subscribe_quotes=True,
            futu_module=FakeFutu(),
            context_factory=lambda: context,
            lock_factory=FakeLock,
            monotonic=clock.monotonic,
            sleeper=clock.sleep,
        )

        self.assertGreaterEqual(
            context.unsubscribe_at - context.subscribe_returned_at,
            61.0,
        )
        self.assertGreaterEqual(
            context.unsubscribe_at - context.subscribe_started_at,
            71.0,
        )
        self.assertEqual(
            result["subscription"]["cleanup_status"], "restored"
        )

    def test_keyboard_interrupt_waits_cleans_and_then_reraises(self):
        class FakeClock:
            def __init__(self):
                self.now = 300.0
                self.interrupted = False

            def monotonic(self):
                return self.now

            def sleep(self, seconds):
                if not self.interrupted:
                    self.interrupted = True
                    raise KeyboardInterrupt
                self.now += seconds

        clock = FakeClock()
        context = DynamicContext()
        lock = FakeLock()

        with self.assertRaises(KeyboardInterrupt):
            run_probe(
                ["US.TSLA"],
                subscribe_quotes=True,
                futu_module=FakeFutu(),
                context_factory=lambda: context,
                lock_factory=lambda: lock,
                monotonic=clock.monotonic,
                sleeper=clock.sleep,
            )

        call_names = [call[0] for call in context.calls]
        self.assertIn("unsubscribe", call_names)
        self.assertGreaterEqual(call_names.count("query_subscription"), 2)
        self.assertTrue(context.closed)
        self.assertTrue(lock.released)
