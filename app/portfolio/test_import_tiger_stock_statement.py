from datetime import date
from decimal import Decimal

from django.core.management.base import CommandError
from django.test import SimpleTestCase

from portfolio.management.commands.import_tiger_stock_statement_2024 import (
    build_opening_rows,
    normalize_payload,
    validate_source_positions,
)


class TigerStockStatementNormalizeTests(SimpleTestCase):
    def payload(self):
        trades = []
        for source_row in range(2, 50):
            trades.append({
                "sourceRow": source_row,
                "symbol": "AAPL",
                "market": "US",
                "exchange": "NASDAQ",
                "action": "開倉" if source_row % 2 == 0 else "平倉",
                "quantity": 1 if source_row % 2 == 0 else -1,
                "price": 10,
                "grossAmount": 10 if source_row % 2 == 0 else -10,
                "fees": 1,
                "feeComponents": {"其他代收": -0.8, "證監會費": -0.2},
                "realizedPnl": 0 if source_row % 2 == 0 else -2,
                "tradeTime": "2024-01-02\n09:30:00, US/Eastern",
                "tradeDate": "2024-01-02",
                "settlementDate": "2024-01-04",
                "currency": "USD",
            })
        cash_rows = [
            {"sourceRow": 69, "movementDate": "2024-06-07", "description": "入金", "amount": 100, "currency": "USD"},
            {"sourceRow": 70, "movementDate": "2024-06-08", "description": "出金", "amount": -20, "currency": "USD"},
            {"sourceRow": 72, "movementDate": "2024-06-09", "description": "入金", "amount": 50, "currency": "HKD"},
            {"sourceRow": 73, "movementDate": "2024-06-10", "description": "出金", "amount": -10, "currency": "HKD"},
        ]
        return {"statementYear": 2024, "trades": trades, "cashMovements": cash_rows}

    def test_normalize_splits_tax_and_fee(self):
        trades, cash_rows = normalize_payload(self.payload())
        self.assertEqual(len(trades), 48)
        self.assertEqual(len(cash_rows), 4)
        self.assertEqual(trades[0]["fee"], Decimal("0.8000"))
        self.assertEqual(trades[0]["tax"], Decimal("0.2000"))
        self.assertEqual(trades[0]["settlement_date"], date(2024, 1, 4))

    def test_normalize_rejects_wrong_quantity_direction(self):
        payload = self.payload()
        payload["trades"][0]["quantity"] = -1
        with self.assertRaisesMessage(CommandError, "数量方向"):
            normalize_payload(payload)

    def test_opening_rows_reproduce_reported_sale_profit(self):
        keys = [("HK", "01810"), ("HK", "02158"), ("US", "NIO"), ("US", "RLX")]
        trades = []
        for source_row, key in enumerate(keys, 2):
            trades.append({
                "source_row": source_row,
                "external_id": f"trade-{source_row}",
                "key": key,
                "trade_date": date(2024, 6, 18),
                "settlement_date": None,
                "trade_type": "sell",
                "quantity": Decimal("10"),
                "price": Decimal("10"),
                "amount": Decimal("100"),
                "fee": Decimal("2"),
                "tax": Decimal("1"),
                "currency": "HKD" if key[0] == "HK" else "USD",
                "reported_realized_pnl": Decimal("10"),
                "trade_time": "",
                "statement_action": "平倉",
                "signed_quantity": Decimal("-10"),
                "signed_gross": Decimal("-100"),
                "fee_components": {},
            })
        openings = build_opening_rows(trades, date(2024, 1, 1))
        self.assertEqual(len(openings), 4)
        self.assertTrue(all(row["amount"] == Decimal("87.0000") for row in openings))
        self.assertTrue(all(row["trade_date"] == date(2024, 1, 1) for row in openings))
        validate_source_positions(trades, openings)

    def test_validate_source_positions_rejects_negative_position(self):
        trades = [{
            "key": ("US", "AAPL"), "trade_date": date(2024, 1, 2),
            "source_row": 2, "external_id": "sell", "quantity": Decimal("1"),
            "trade_type": "sell",
        }]
        with self.assertRaisesMessage(CommandError, "负持仓"):
            validate_source_positions(trades, [])
