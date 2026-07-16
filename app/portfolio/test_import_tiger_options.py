from decimal import Decimal

from django.test import SimpleTestCase

from portfolio.management.commands.import_tiger_option_history import (
    allocate_group_fees,
    positions_due_for_expiry,
    remaining_positions,
)


class TigerOptionImportHelpersTest(SimpleTestCase):
    def test_fees_are_allocated_by_contract_quantity_without_losing_total(self):
        rows = [
            {"source_row": 2, "underlying_symbol": "MSFT", "quantity": "2"},
            {"source_row": 3, "underlying_symbol": "MSFT", "quantity": "1"},
            {"source_row": 4, "underlying_symbol": "MSFT", "quantity": "3"},
        ]

        result = allocate_group_fees(rows, {"MSFT": "13.27"})

        self.assertEqual(sum(result.values()), Decimal("13.2700"))
        self.assertEqual(result[2], Decimal("4.4233"))
        self.assertEqual(result[3], Decimal("2.2117"))
        self.assertEqual(result[4], Decimal("6.6350"))

    def test_remaining_positions_keep_long_and_short_signs(self):
        rows = [
            {"contract_symbol": "LONG", "trade_type": "buy", "quantity": "2"},
            {"contract_symbol": "LONG", "trade_type": "sell", "quantity": "1"},
            {"contract_symbol": "SHORT", "trade_type": "sell", "quantity": "1"},
            {"contract_symbol": "CLOSED", "trade_type": "sell", "quantity": "1"},
            {"contract_symbol": "CLOSED", "trade_type": "buy", "quantity": "1"},
        ]

        self.assertEqual(
            remaining_positions(rows),
            {"LONG": Decimal("1"), "SHORT": Decimal("-1")},
        )

    def test_expiry_closes_do_not_close_future_contracts(self):
        rows = [
            {
                "contract_symbol": "EXPIRED",
                "trade_type": "sell",
                "quantity": "1",
                "expiration_date": "2026-06-18",
            },
            {
                "contract_symbol": "FUTURE",
                "trade_type": "sell",
                "quantity": "1",
                "expiration_date": "2026-07-31",
            },
        ]

        self.assertEqual(
            positions_due_for_expiry(rows, "2026-07-15"),
            {"EXPIRED": Decimal("-1")},
        )
