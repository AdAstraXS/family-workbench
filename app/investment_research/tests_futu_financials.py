"""Provider data remains a separate, explicitly refreshed financial reference."""
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase, override_settings

from .futu_financials import (
    FutuFinancialError, _breakdown_data, _statement_data, comparison_rows,
    fetch_futu_financials, highlight_rows, provider_code, statement_tables,
)


class FutuFinancialTests(SimpleTestCase):
    def test_provider_code_does_not_guess_unsupported_exchange(self):
        self.assertEqual(provider_code(SimpleNamespace(market="US", symbol="MSFT")), "US.MSFT")
        self.assertEqual(provider_code(SimpleNamespace(market="HK", symbol="700")), "HK.00700")
        with self.assertRaises(FutuFinancialError):
            provider_code(SimpleNamespace(market="OTHER", symbol="MSFT"))

    def test_sdk_floats_become_decimal_strings_and_only_fy_is_kept(self):
        reports = _statement_data({"structure_list": [
            {"field_id": 8001, "display_name": "营业总收入"},
        ], "report_list": [
            {"period_text": "2025/FY", "date_time_str": "2025-06-30", "currency_code": "USD",
             "item_list": [{"field_id": 8001,
                            "data": 123456789.25, "yoy": 12.5}]},
            {"period_text": "2025/Q3", "item_list": [{"data": 99}]},
        ]})
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]["items"][0]["amount"], "123456789.25")
        self.assertEqual(reports[0]["items"][0]["yoy"], "12.5")
        self.assertEqual(reports[0]["items"][0]["name"], "营业总收入")

    def test_provider_fields_are_named_aligned_and_formatted(self):
        reports = _statement_data({"structure_list": [
            {"field_id": 8001, "display_name": "营业总收入"},
        ], "report_list": [
            {"period_text": "2026/FY", "date_time_str": "2026-06-29", "currency_code": "USD",
             "item_list": [{"field_id": 8001, "data": 331839000000.0, "yoy": 17.7886}]},
            {"period_text": "2025/FY", "date_time_str": "2025-06-29", "currency_code": "USD",
             "item_list": [{"field_id": 8001, "data": 281724000000.0}]},
        ]})
        tables = statement_tables([{"type": 1, "title": "利润表", "reports": reports}])
        self.assertEqual(tables[0]["rows"][0]["name"], "营业总收入")
        self.assertEqual(tables[0]["rows"][0]["cells"][0]["amount"], "3,318.39 亿美元")
        self.assertEqual(tables[0]["rows"][0]["cells"][0]["yoy"], "17.79%")
        self.assertEqual(highlight_rows(tables)[0]["label"], "营业收入")

    def test_breakdown_type_from_sdk_string_is_readable(self):
        breakdown = _breakdown_data({"period": "2026/FY", "currency_code": "USD",
                                     "breakdown_list": [{"type": "RevenueBreakdownType_Region",
                                                         "item_list": []}]})
        self.assertEqual(breakdown["groups"][0]["type"], "地区")

    @override_settings(FUTU_OPEND_HOST="127.0.0.1", FUTU_OPEND_PORT=11111)
    @mock.patch("investment_research.futu_financials.socket.create_connection")
    def test_fetch_closes_context_and_keeps_breakdown_failure_separate(self, connection):
        context = mock.Mock()
        context.get_financials_statements.return_value = (0, {"report_list": [
            {"period_text": "2025/FY", "date_time_str": "2025-06-30",
             "currency_code": "USD", "item_list": [{"field_id": 5001,
             "display_name": "Total Revenue", "data": 100000000.0}]},
        ]})
        context.get_financials_revenue_breakdown.return_value = (-1, "not entitled")
        result = fetch_futu_financials("US.MSFT", context_factory=lambda **kw: context)
        self.assertEqual(len(result["statements"]), 4)
        self.assertEqual(result["breakdown_error"], "not entitled")
        context.close.assert_called_once()
        connection.return_value.close.assert_called_once()

    def test_comparison_requires_same_period_and_currency(self):
        snapshot = SimpleNamespace(data={"statements": [{"type": 1, "reports": [
            {"period_end": "2025-06-30", "currency": "USD",
             "items": [{"field_id": 5001, "name": "Total Revenue", "amount": "12000000000"}]},
            {"period_end": "2024-06-30", "currency": "CNY",
             "items": [{"field_id": 5001, "name": "Total Revenue", "amount": "1"}]},
        ]}]})
        rows = [{"code": "revenue", "label": "营业收入", "unit": "money",
                 "cells": [{"amount": Decimal(110)}, {"amount": Decimal(120)}]}]
        result = comparison_rows(snapshot, [date(2024, 6, 30), date(2025, 6, 30)], rows)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["futu"], Decimal(120))
