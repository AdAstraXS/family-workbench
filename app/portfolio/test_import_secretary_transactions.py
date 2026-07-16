from decimal import Decimal

from django.test import SimpleTestCase

from portfolio.management.commands.import_secretary_transaction_history import normalize_rows
from portfolio.models import TradeTypeChoices


class SecretaryTransactionImportHelpersTest(SimpleTestCase):
    def test_rows_are_normalized_and_bond_quantity_becomes_face_value(self):
        payload = {"values": [
            [
                "交易日期", "交易类型", "代码", "名称", "价格", "数量", "成交金额",
                "手续费", "现金变动", "券商", "币种", "用户", "资产类别",
            ],
            [
                "2025.06.05", "买入", "912810TV0", "US-T", 97.562, 20, 1951.24,
                13.08, -1964.32, "盈利香港", "USD", "孙秘书", "固定收益类",
            ],
            [
                "2025.06.30", "卖出", "09961", "携程集团-S", 462, -50, -23100,
                41.93, 23058.07, "长桥证券", "HKD", "孙秘书", "权益类",
            ],
        ]}

        bond, stock = normalize_rows(payload)

        self.assertEqual(bond["quantity"], Decimal("2000.000000"))
        self.assertEqual(bond["amount"], Decimal("1951.2400"))
        self.assertEqual(stock["trade_type"], TradeTypeChoices.SELL)
        self.assertEqual(stock["quantity"], Decimal("50.000000"))
        self.assertEqual(stock["external_id"], "secretary-trade-history-715:row-3")
