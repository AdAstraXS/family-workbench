import json
from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from portfolio.market_data import ensure_quote_config
from portfolio.models import (
    CashMovementTypeChoices,
    InvestmentAccount,
    InvestmentCashMovement,
    InvestmentTransaction,
    Security,
    TradeStatusChoices,
    TradeTypeChoices,
    TransactionSourceChoices,
)
from portfolio.services import rebuild_position


IMPORT_PREFIX = "tiger-stock-statement-2024"
MONEY_STEP = Decimal("0.0001")
PRICE_STEP = Decimal("0.000001")
QUANTITY_STEP = Decimal("0.000001")
TAX_COMPONENTS = {
    "印花稅",
    "交易征費",
    "證監會費",
    "會計及財匯局交易征費",
}
APPROVED_OPENINGS = {
    ("HK", "01810"),
    ("HK", "02158"),
    ("US", "NIO"),
    ("US", "RLX"),
}
SECURITY_SPECS = {
    ("HK", "01810"): {
        "name": "小米集团-W", "exchange": "HK", "asset_type": Security.TYPE_STOCK,
        "currency": "HKD", "lot_size": 200, "listing_date": "2018-07-09",
    },
    ("HK", "02158"): {
        "name": "医渡科技", "exchange": "HK", "asset_type": Security.TYPE_STOCK,
        "currency": "HKD", "lot_size": 100, "listing_date": "2021-01-15",
    },
    ("HK", "09988"): {
        "name": "阿里巴巴-W", "exchange": "HK", "asset_type": Security.TYPE_STOCK,
        "currency": "HKD", "lot_size": 100, "listing_date": "2019-11-26",
    },
    ("US", "AAPL"): {
        "name": "苹果", "exchange": "NASDAQ", "asset_type": Security.TYPE_STOCK,
        "currency": "USD", "lot_size": 1, "listing_date": None,
    },
    ("US", "MSFT"): {
        "name": "微软", "exchange": "NASDAQ", "asset_type": Security.TYPE_STOCK,
        "currency": "USD", "lot_size": 1, "listing_date": None,
    },
    ("US", "NIO"): {
        "name": "蔚来", "exchange": "NYSE", "asset_type": Security.TYPE_STOCK,
        "currency": "USD", "lot_size": 1, "listing_date": "2018-09-12",
    },
    ("US", "NVDA"): {
        "name": "英伟达", "exchange": "NASDAQ", "asset_type": Security.TYPE_STOCK,
        "currency": "USD", "lot_size": 1, "listing_date": None,
    },
    ("US", "PONY"): {
        "name": "小马智行", "exchange": "NASDAQ", "asset_type": Security.TYPE_STOCK,
        "currency": "USD", "lot_size": 1, "listing_date": "2024-11-27",
    },
    ("US", "RLX"): {
        "name": "雾芯科技", "exchange": "NYSE", "asset_type": Security.TYPE_STOCK,
        "currency": "USD", "lot_size": 1, "listing_date": "2021-01-22",
    },
    ("US", "TNA"): {
        "name": "三倍做多小盘股ETF-Direxion", "exchange": "ARCA", "asset_type": Security.TYPE_ETF,
        "currency": "USD", "lot_size": 1, "listing_date": None,
    },
    ("US", "TQQQ"): {
        "name": "三倍做多纳指ETF-ProShares", "exchange": "NASDAQ", "asset_type": Security.TYPE_ETF,
        "currency": "USD", "lot_size": 1, "listing_date": None,
    },
    ("US", "TSLA"): {
        "name": "特斯拉", "exchange": "NASDAQ", "asset_type": Security.TYPE_STOCK,
        "currency": "USD", "lot_size": 1, "listing_date": None,
    },
    ("US", "XLU"): {
        "name": "公用事业精选行业指数ETF-SPDR", "exchange": "ARCA", "asset_type": Security.TYPE_ETF,
        "currency": "USD", "lot_size": 1, "listing_date": None,
    },
    ("US", "XLV"): {
        "name": "医疗保健精选行业指数ETF-SPDR", "exchange": "ARCA", "asset_type": Security.TYPE_ETF,
        "currency": "USD", "lot_size": 1, "listing_date": None,
    },
}


def decimal_value(value, step=MONEY_STEP):
    try:
        return Decimal(str(value or 0)).quantize(step, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise CommandError(f"无效数字：{value}") from exc


def iso_date(value, label):
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise CommandError(f"无效{label}：{value}") from exc


def normalize_payload(payload):
    if payload.get("statementYear") != 2024:
        raise CommandError("仅支持已经复核的 2024 年老虎证券结单。")
    raw_trades = payload.get("trades") or []
    raw_cash = payload.get("cashMovements") or []
    if len(raw_trades) != 48 or len(raw_cash) != 4:
        raise CommandError(
            f"结单行数不符：成交={len(raw_trades)}（应为48），出入金={len(raw_cash)}（应为4）。"
        )

    trades = []
    seen_rows = set()
    for raw in raw_trades:
        source_row = int(raw.get("sourceRow") or 0)
        if not source_row or source_row in seen_rows:
            raise CommandError(f"成交来源行无效或重复：{source_row}")
        seen_rows.add(source_row)
        key = (str(raw.get("market") or "").strip(), str(raw.get("symbol") or "").strip().upper())
        if key not in SECURITY_SPECS:
            raise CommandError(f"未复核的证券标的：{key[0]} {key[1]}")
        action = str(raw.get("action") or "").strip()
        trade_type = {"開倉": TradeTypeChoices.BUY, "平倉": TradeTypeChoices.SELL}.get(action)
        if not trade_type:
            raise CommandError(f"第 {source_row} 行交易类型不支持：{action}")
        signed_quantity = decimal_value(raw.get("quantity"), QUANTITY_STEP)
        signed_gross = decimal_value(raw.get("grossAmount"))
        if not signed_quantity or (
            trade_type == TradeTypeChoices.BUY and signed_quantity < 0
        ) or (
            trade_type == TradeTypeChoices.SELL and signed_quantity > 0
        ):
            raise CommandError(f"第 {source_row} 行数量方向与开平仓不一致。")
        if (
            trade_type == TradeTypeChoices.BUY and signed_gross < 0
        ) or (
            trade_type == TradeTypeChoices.SELL and signed_gross > 0
        ):
            raise CommandError(f"第 {source_row} 行成交额方向与开平仓不一致。")

        quantity = abs(signed_quantity)
        price = decimal_value(raw.get("price"), PRICE_STEP)
        amount = abs(signed_gross)
        if abs(amount - quantity * price) > Decimal("0.0100"):
            raise CommandError(f"第 {source_row} 行成交额不等于数量乘价格。")

        fee_components = raw.get("feeComponents") or {}
        fee = Decimal("0")
        tax = Decimal("0")
        for name, source_amount in fee_components.items():
            component = decimal_value(source_amount)
            if component > 0:
                raise CommandError(f"第 {source_row} 行费用分项应为负数：{name}")
            if name in TAX_COMPONENTS:
                tax += abs(component)
            else:
                fee += abs(component)
        statement_fees = decimal_value(raw.get("fees"))
        if abs(statement_fees - fee - tax) > MONEY_STEP:
            raise CommandError(
                f"第 {source_row} 行费用合计不符：{statement_fees} != {fee + tax}"
            )

        currency = str(raw.get("currency") or "").strip().upper()
        if currency != SECURITY_SPECS[key]["currency"]:
            raise CommandError(f"第 {source_row} 行币种不符：{currency}")
        reported_pnl = (
            decimal_value(raw.get("realizedPnl"))
            if raw.get("realizedPnl") is not None
            else None
        )
        trades.append({
            "source_row": source_row,
            "external_id": f"{IMPORT_PREFIX}:trade:row-{source_row}",
            "key": key,
            "trade_date": iso_date(raw.get("tradeDate"), "交易日期"),
            "settlement_date": (
                iso_date(raw.get("settlementDate"), "交收日期")
                if raw.get("settlementDate") else None
            ),
            "trade_type": trade_type,
            "quantity": quantity,
            "price": price,
            "amount": amount,
            "fee": fee.quantize(MONEY_STEP),
            "tax": tax.quantize(MONEY_STEP),
            "currency": currency,
            "reported_realized_pnl": reported_pnl,
            "trade_time": str(raw.get("tradeTime") or ""),
            "statement_action": action,
            "signed_quantity": signed_quantity,
            "signed_gross": signed_gross,
            "fee_components": fee_components,
        })
    trades.sort(key=lambda row: (row["trade_date"], row["source_row"]))

    cash_rows = []
    seen_cash_rows = set()
    for raw in raw_cash:
        source_row = int(raw.get("sourceRow") or 0)
        if not source_row or source_row in seen_cash_rows:
            raise CommandError(f"出入金来源行无效或重复：{source_row}")
        seen_cash_rows.add(source_row)
        description = str(raw.get("description") or "").strip()
        movement_type = {
            "入金": CashMovementTypeChoices.DEPOSIT,
            "出金": CashMovementTypeChoices.WITHDRAWAL,
        }.get(description)
        if not movement_type:
            raise CommandError(f"第 {source_row} 行出入金类型不支持：{description}")
        amount = decimal_value(raw.get("amount"))
        if not amount or (
            movement_type == CashMovementTypeChoices.DEPOSIT and amount < 0
        ) or (
            movement_type == CashMovementTypeChoices.WITHDRAWAL and amount > 0
        ):
            raise CommandError(f"第 {source_row} 行出入金金额方向不符。")
        cash_rows.append({
            "source_row": source_row,
            "external_id": f"{IMPORT_PREFIX}:cash:row-{source_row}",
            "movement_date": iso_date(raw.get("movementDate"), "出入金日期"),
            "movement_type": movement_type,
            "amount": amount,
            "currency": str(raw.get("currency") or "").strip().upper(),
            "description": description,
        })
    cash_rows.sort(key=lambda row: (row["movement_date"], row["source_row"]))
    return trades, cash_rows


def build_opening_rows(trades, opening_date):
    grouped = defaultdict(list)
    for row in trades:
        grouped[row["key"]].append(row)
    openings = []
    for key in APPROVED_OPENINGS:
        rows = grouped.get(key) or []
        if len(rows) != 1 or rows[0]["trade_type"] != TradeTypeChoices.SELL:
            raise CommandError(f"{key[0]} {key[1]} 的期初持仓推算条件已变化，请重新人工复核。")
        sale = rows[0]
        if sale["reported_realized_pnl"] is None:
            raise CommandError(f"{key[0]} {key[1]} 缺少结单已实现盈亏，无法推算期初成本。")
        cost = (
            sale["amount"] - sale["fee"] - sale["tax"]
            - sale["reported_realized_pnl"]
        ).quantize(MONEY_STEP)
        if cost <= 0:
            raise CommandError(f"{key[0]} {key[1]} 反推的期初成本无效：{cost}")
        openings.append({
            "source_row": sale["source_row"],
            "external_id": f"{IMPORT_PREFIX}:opening:{key[0]}:{key[1]}",
            "key": key,
            "trade_date": opening_date,
            "settlement_date": None,
            "trade_type": TradeTypeChoices.BUY,
            "quantity": sale["quantity"],
            "price": (cost / sale["quantity"]).quantize(PRICE_STEP),
            "amount": cost,
            "fee": Decimal("0"),
            "tax": Decimal("0"),
            "currency": sale["currency"],
            "reported_realized_pnl": None,
            "trade_time": "",
            "statement_action": "推算期初持仓",
            "signed_quantity": sale["quantity"],
            "signed_gross": cost,
            "fee_components": {},
            "opening_inferred": True,
            "source_sale_row": sale["source_row"],
        })
    openings.sort(key=lambda row: row["key"])
    return openings


def validate_source_positions(trades, openings):
    grouped = defaultdict(list)
    for row in openings + trades:
        grouped[row["key"]].append(row)
    for key, rows in grouped.items():
        balance = Decimal("0")
        for row in sorted(rows, key=lambda item: (item["trade_date"], item["source_row"], item["external_id"])):
            balance += row["quantity"] if row["trade_type"] == TradeTypeChoices.BUY else -row["quantity"]
            if balance < 0:
                raise CommandError(f"{key[0]} {key[1]} 在 {row['trade_date']} 出现负持仓。")
        if balance:
            raise CommandError(f"{key[0]} {key[1]} 导入后结单期末持仓不为零：{balance}")


class Command(BaseCommand):
    help = "导入已经复核的老虎证券 2024 年股票/ETF成交及出入金结单；默认仅 dry-run。"

    def add_arguments(self, parser):
        parser.add_argument("json_path")
        parser.add_argument("--member", default="我")
        parser.add_argument("--account", default="老虎证券")
        parser.add_argument("--opening-date", default="2024-01-01")
        parser.add_argument("--commit", action="store_true")

    def handle(self, *args, **options):
        path = Path(options["json_path"])
        if not path.exists():
            raise CommandError(f"导入文件不存在：{path}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        trades, cash_rows = normalize_payload(payload)
        opening_date = iso_date(options["opening_date"], "期初持仓日期")
        openings = build_opening_rows(trades, opening_date)
        validate_source_positions(trades, openings)
        account = self._account(options["member"], options["account"])
        securities, missing_keys = self._existing_securities(account, trades)

        all_transaction_rows = openings + trades
        transaction_ids = [row["external_id"] for row in all_transaction_rows]
        cash_ids = [row["external_id"] for row in cash_rows]
        existing_transaction_ids = set(
            InvestmentTransaction.objects.filter(
                account=account,
                source=TransactionSourceChoices.IMPORT,
                external_id__in=transaction_ids,
            ).values_list("external_id", flat=True)
        )
        existing_cash_ids = set(
            InvestmentCashMovement.objects.filter(
                account=account,
                source=TransactionSourceChoices.IMPORT,
                external_id__in=cash_ids,
            ).values_list("external_id", flat=True)
        )
        mode = "COMMIT" if options["commit"] else "DRY-RUN"
        self.stdout.write(
            f"mode={mode} trades={len(trades)} openings={len(openings)} cash={len(cash_rows)} "
            f"transaction_create={len(transaction_ids) - len(existing_transaction_ids)} "
            f"transaction_update={len(existing_transaction_ids)} "
            f"cash_create={len(cash_ids) - len(existing_cash_ids)} cash_update={len(existing_cash_ids)} "
            f"missing_securities={len(missing_keys)}"
        )
        for row in openings:
            self.stdout.write(
                f"opening {row['key'][0]} {row['key'][1]} date={row['trade_date']} "
                f"quantity={row['quantity']} cost={row['amount']} {row['currency']}"
            )
        for currency in ("HKD", "USD"):
            currency_trades = [row for row in trades if row["currency"] == currency]
            trade_cash = sum(
                (
                    -(row["amount"] + row["fee"] + row["tax"])
                    if row["trade_type"] == TradeTypeChoices.BUY
                    else row["amount"] - row["fee"] - row["tax"]
                )
                for row in currency_trades
            )
            standalone_cash = sum(
                (row["amount"] for row in cash_rows if row["currency"] == currency),
                Decimal("0"),
            )
            self.stdout.write(
                f"{currency}: trade_rows={len(currency_trades)} trade_cash={trade_cash:.4f} "
                f"standalone_cash={standalone_cash:.4f}"
            )
        if missing_keys:
            self.stdout.write("待新增证券：" + ", ".join(f"{market}:{symbol}" for market, symbol in missing_keys))
        if not options["commit"]:
            self.stdout.write(self.style.WARNING("dry-run 完成；使用 --commit 才会写入数据库。"))
            return

        with transaction.atomic():
            securities, _ = self._ensure_securities(account, trades)
            touched = set()
            created_transactions = 0
            updated_transactions = 0
            imported_items = {}
            for row in all_transaction_rows:
                security = securities[row["key"]]
                opening = bool(row.get("opening_inferred"))
                item, created = InvestmentTransaction.objects.update_or_create(
                    account=account,
                    source=TransactionSourceChoices.IMPORT,
                    external_id=row["external_id"],
                    defaults={
                        "security": security,
                        "asset_category": security.asset_category,
                        "trade_date": row["trade_date"],
                        "trade_type": row["trade_type"],
                        "position_effect": "",
                        "status": TradeStatusChoices.COMPLETED,
                        "quantity": row["quantity"],
                        "price": row["price"],
                        "amount": row["amount"],
                        "fee": row["fee"],
                        "tax": row["tax"],
                        "currency": row["currency"],
                        "remark": (
                            "根据老虎证券2024年结单已实现盈亏反推的期初持仓；"
                            "用户确认日期为2024-01-01。"
                            if opening else "老虎证券2024年股票及ETF结单导入"
                        ),
                        "extra_data": {
                            "tiger_stock_statement_2024": True,
                            "source_sheet": payload.get("sourceSheet", ""),
                            "source_row": row["source_row"],
                            "source_trade_time": row["trade_time"],
                            "source_settlement_date": (
                                row["settlement_date"].isoformat()
                                if row["settlement_date"] else ""
                            ),
                            "source_action": row["statement_action"],
                            "source_signed_quantity": str(row["signed_quantity"]),
                            "source_signed_gross": str(row["signed_gross"]),
                            "source_fee_components": row["fee_components"],
                            "source_reported_realized_pnl": (
                                str(row["reported_realized_pnl"])
                                if row["reported_realized_pnl"] is not None else ""
                            ),
                            "opening_position_inferred": opening,
                            "opening_inference_user_confirmed": opening,
                            "opening_source_sale_row": row.get("source_sale_row"),
                        },
                    },
                )
                created_transactions += int(created)
                updated_transactions += int(not created)
                touched.add(security.pk)
                imported_items[row["external_id"]] = (item, row)

            for security_id in touched:
                rebuild_position(account, Security.objects.get(pk=security_id))
            for item, row in imported_items.values():
                if row["settlement_date"]:
                    InvestmentCashMovement.objects.filter(transaction=item).update(
                        settlement_date=row["settlement_date"]
                    )

            created_cash = 0
            updated_cash = 0
            for row in cash_rows:
                _, created = InvestmentCashMovement.objects.update_or_create(
                    account=account,
                    source=TransactionSourceChoices.IMPORT,
                    external_id=row["external_id"],
                    defaults={
                        "transaction": None,
                        "movement_date": row["movement_date"],
                        "settlement_date": None,
                        "movement_type": row["movement_type"],
                        "currency": row["currency"],
                        "amount": row["amount"],
                        "counterparty_account": None,
                        "remark": f"老虎证券2024年结单{row['description']}（来源行 {row['source_row']}）",
                    },
                )
                created_cash += int(created)
                updated_cash += int(not created)

            self._verify_realized_pnl(account, trades)

        self.stdout.write(self.style.SUCCESS(
            f"导入完成：交易新增={created_transactions}、更新={updated_transactions}，"
            f"独立现金流水新增={created_cash}、更新={updated_cash}，重建标的={len(touched)}。"
        ))

    @staticmethod
    def _account(member_name, account_name):
        accounts = list(
            InvestmentAccount.objects.select_related("bank_account__member__family").filter(
                bank_account__member__display_name=member_name,
                bank_account__account_name=account_name,
                bank_account__is_active=True,
            )
        )
        if len(accounts) != 1:
            raise CommandError(
                f"投资账户匹配数量应为1，实际为{len(accounts)}：{member_name} / {account_name}"
            )
        return accounts[0]

    @staticmethod
    def _existing_securities(account, trades):
        keys = sorted({row["key"] for row in trades})
        securities = {
            (item.market, item.symbol): item
            for item in Security.objects.filter(
                market__in={key[0] for key in keys},
                symbol__in={key[1] for key in keys},
            ).select_related("asset_category")
        }
        for key, security in securities.items():
            spec = SECURITY_SPECS[key]
            if security.currency != spec["currency"] or security.asset_type != spec["asset_type"]:
                raise CommandError(
                    f"证券属性不符：{key[0]} {key[1]}，"
                    f"实际={security.asset_type}/{security.currency}，"
                    f"应为={spec['asset_type']}/{spec['currency']}"
                )
        return securities, [key for key in keys if key not in securities]

    @classmethod
    def _ensure_securities(cls, account, trades):
        securities, missing_keys = cls._existing_securities(account, trades)
        for key in missing_keys:
            spec = SECURITY_SPECS[key]
            listing_date = (
                date.fromisoformat(spec["listing_date"])
                if spec["listing_date"] else None
            )
            security = Security.objects.create(
                symbol=key[1],
                name=spec["name"],
                market=key[0],
                exchange=spec["exchange"],
                asset_type=spec["asset_type"],
                asset_category=Security.default_asset_category(account.family, spec["asset_type"]),
                currency=spec["currency"],
                lot_size=spec["lot_size"],
                listing_date=listing_date,
                data_source="futu",
                is_active=True,
                extra_data={
                    "futu_code": f"{key[0]}.{key[1]}",
                    "created_by_import": IMPORT_PREFIX,
                },
            )
            ensure_quote_config(security)
            securities[key] = security
        return securities, missing_keys

    @staticmethod
    def _verify_realized_pnl(account, trades):
        expected = defaultdict(lambda: Decimal("0"))
        external_ids = []
        for row in trades:
            external_ids.append(row["external_id"])
            if row["reported_realized_pnl"] is not None:
                expected[row["key"]] += row["reported_realized_pnl"]
        actual = defaultdict(lambda: Decimal("0"))
        for item in InvestmentTransaction.objects.filter(
            account=account,
            source=TransactionSourceChoices.IMPORT,
            external_id__in=external_ids,
            trade_type=TradeTypeChoices.SELL,
        ).select_related("security"):
            actual[(item.security.market, item.security.symbol)] += item.realized_pnl
        for key, expected_value in expected.items():
            if abs(actual[key] - expected_value) > Decimal("0.0200"):
                raise CommandError(
                    f"{key[0]} {key[1]} 已实现盈亏未与结单勾稽："
                    f"系统={actual[key]}，结单={expected_value}"
                )
