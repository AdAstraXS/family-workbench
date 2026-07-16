import json
from collections import defaultdict
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from portfolio.models import (
    BondDetail,
    InvestmentAccount,
    InvestmentTransaction,
    Security,
    TradeStatusChoices,
    TradeTypeChoices,
    TransactionSourceChoices,
)
from portfolio.services import rebuild_position


IMPORT_PREFIX = "secretary-trade-history-715"
ACCOUNT_MARKETS = {
    "长桥证券": "HK",
    "盈利香港": "US",
    "盈利新加坡": "US",
}
MONEY_STEP = Decimal("0.0001")
QUANTITY_STEP = Decimal("0.000001")
PRICE_STEP = Decimal("0.000001")


def decimal_value(value, step):
    return Decimal(str(value or 0)).quantize(step, rounding=ROUND_HALF_UP)


def parse_trade_date(value):
    try:
        year, month, day = (int(part) for part in str(value).split("."))
        return date(year, month, day)
    except (TypeError, ValueError) as exc:
        raise CommandError(f"Invalid trade date: {value}") from exc


def normalize_rows(payload):
    values = payload.get("values") or []
    if not values:
        raise CommandError("No spreadsheet values found.")
    headers = values[0]
    required = {
        "交易日期", "交易类型", "代码", "名称", "价格", "数量", "成交金额",
        "手续费", "现金变动", "券商", "币种", "用户", "资产类别",
    }
    if not required.issubset(headers):
        raise CommandError(f"Missing columns: {sorted(required - set(headers))}")

    records = []
    seen = set()
    for source_row, row_values in enumerate(values[1:], 2):
        row = dict(zip(headers, row_values))
        if not row.get("交易日期"):
            continue
        if row.get("用户") != "孙秘书":
            raise CommandError(f"Row {source_row}: unexpected member {row.get('用户')}")
        broker = row.get("券商")
        if broker not in ACCOUNT_MARKETS:
            raise CommandError(f"Row {source_row}: unsupported broker {broker}")
        trade_type = {
            "买入": TradeTypeChoices.BUY,
            "卖出": TradeTypeChoices.SELL,
        }.get(row.get("交易类型"))
        if not trade_type:
            raise CommandError(f"Row {source_row}: unsupported trade type {row.get('交易类型')}")

        source_quantity = decimal_value(row.get("数量"), QUANTITY_STEP)
        if not source_quantity or (
            trade_type == TradeTypeChoices.BUY and source_quantity < 0
        ) or (
            trade_type == TradeTypeChoices.SELL and source_quantity > 0
        ):
            raise CommandError(f"Row {source_row}: quantity sign does not match trade type")
        quantity = abs(source_quantity)
        if row.get("资产类别") == "固定收益类":
            quantity *= Decimal("100")

        price = decimal_value(row.get("价格"), PRICE_STEP)
        amount = abs(decimal_value(row.get("成交金额"), MONEY_STEP))
        fee = decimal_value(row.get("手续费"), MONEY_STEP)
        source_cash = decimal_value(row.get("现金变动"), MONEY_STEP)
        expected_cash = -(amount + fee) if trade_type == TradeTypeChoices.BUY else amount - fee
        if source_cash != expected_cash:
            raise CommandError(
                f"Row {source_row}: cash change {source_cash} != expected {expected_cash}"
            )

        external_id = f"{IMPORT_PREFIX}:row-{source_row}"
        if external_id in seen:
            raise CommandError(f"Duplicate source row: {source_row}")
        seen.add(external_id)
        records.append({
            "source_row": source_row,
            "external_id": external_id,
            "trade_date": parse_trade_date(row["交易日期"]),
            "trade_type": trade_type,
            "symbol": str(row["代码"]).strip(),
            "name": str(row["名称"]).strip(),
            "market": ACCOUNT_MARKETS[broker],
            "account_name": broker,
            "asset_category_label": row["资产类别"],
            "quantity": quantity.quantize(QUANTITY_STEP),
            "source_quantity": source_quantity,
            "price": price,
            "amount": amount,
            "fee": fee,
            "source_cash_change": source_cash,
            "currency": str(row["币种"]).strip(),
        })
    if not records:
        raise CommandError("No transaction rows found.")
    return records


class Command(BaseCommand):
    help = "Import Sun Secretary account transactions from the reviewed 2026-07-15 workbook."

    def add_arguments(self, parser):
        parser.add_argument("json_path")
        parser.add_argument("--commit", action="store_true")

    def handle(self, *args, **options):
        path = Path(options["json_path"])
        if not path.exists():
            raise CommandError(f"Import file not found: {path}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        records = normalize_rows(payload)
        accounts = self._accounts(records)
        securities = self._securities(records)
        self._validate_positions(records, accounts, securities)

        existing_ids = set(InvestmentTransaction.objects.filter(
            source=TransactionSourceChoices.IMPORT,
            external_id__in=[row["external_id"] for row in records],
        ).values_list("external_id", flat=True))
        mode = "COMMIT" if options["commit"] else "DRY-RUN"
        self.stdout.write(
            f"mode={mode} rows={len(records)} create={len(records) - len(existing_ids)} "
            f"update={len(existing_ids)} accounts={len(accounts)} securities={len(securities)}"
        )
        for account_name in ACCOUNT_MARKETS:
            rows = [row for row in records if row["account_name"] == account_name]
            self.stdout.write(
                f"{account_name}: rows={len(rows)} buys={sum(row['trade_type'] == TradeTypeChoices.BUY for row in rows)} "
                f"sells={sum(row['trade_type'] == TradeTypeChoices.SELL for row in rows)} "
                f"fees={sum((row['fee'] for row in rows), Decimal('0')):.4f}"
            )
        if not options["commit"]:
            self.stdout.write(self.style.WARNING("Dry run only; use --commit to write."))
            return

        touched = set()
        created_count = 0
        updated_count = 0
        accounts_by_id = {account.pk: account for account in accounts.values()}
        securities_by_id = {security.pk: security for security in securities.values()}
        with transaction.atomic():
            for row in records:
                account = accounts[row["account_name"]]
                security = securities[(row["market"], row["symbol"])]
                _, created = InvestmentTransaction.objects.update_or_create(
                    account=account,
                    source=TransactionSourceChoices.IMPORT,
                    external_id=row["external_id"],
                    defaults={
                        "security": security,
                        "asset_category": security.asset_category,
                        "trade_date": row["trade_date"],
                        "trade_type": row["trade_type"],
                        "status": TradeStatusChoices.COMPLETED,
                        "quantity": row["quantity"],
                        "price": row["price"],
                        "amount": row["amount"],
                        "fee": row["fee"],
                        "tax": Decimal("0"),
                        "currency": row["currency"],
                        "remark": "孙秘书历史交易导入",
                        "extra_data": {
                            "historical_workbook_import": True,
                            "source_file": "交易记录-待上传715.xlsx",
                            "source_sheet": "7.15待上传交易记录",
                            "source_row": row["source_row"],
                            "source_quantity": str(row["source_quantity"]),
                            "source_cash_change": str(row["source_cash_change"]),
                            "bond_quantity_converted_to_face_value": row["asset_category_label"] == "固定收益类",
                        },
                    },
                )
                created_count += int(created)
                updated_count += int(not created)
                touched.add((account.pk, security.pk))

            for account_id, security_id in touched:
                rebuild_position(accounts_by_id[account_id], securities_by_id[security_id])

        self.stdout.write(self.style.SUCCESS(
            f"Imported created={created_count}, updated={updated_count}, positions={len(touched)}."
        ))

    @staticmethod
    def _accounts(records):
        names = {row["account_name"] for row in records}
        accounts = InvestmentAccount.objects.select_related("bank_account__member__family").filter(
            bank_account__member__display_name="孙秘书",
            bank_account__account_name__in=names,
            bank_account__is_active=True,
        )
        result = {account.account_name: account for account in accounts}
        missing = names - result.keys()
        if missing:
            raise CommandError(f"Investment accounts not found: {sorted(missing)}")
        return result

    @staticmethod
    def _securities(records):
        result = {}
        for row in records:
            key = (row["market"], row["symbol"])
            if key in result:
                continue
            try:
                security = Security.objects.select_related("asset_category", "bond_detail").get(
                    market=row["market"], symbol=row["symbol"]
                )
            except Security.DoesNotExist as exc:
                raise CommandError(f"Security not found: {row['market']} {row['symbol']}") from exc
            if security.currency != row["currency"]:
                raise CommandError(
                    f"Currency mismatch for {security.symbol}: {security.currency} != {row['currency']}"
                )
            if row["asset_category_label"] == "固定收益类" and security.asset_type != Security.TYPE_BOND:
                raise CommandError(f"Expected bond security: {security.symbol}")
            if row["asset_category_label"] == "固定收益类" and (
                not hasattr(security, "bond_detail")
                or security.bond_detail.quote_basis != BondDetail.PER_100
            ):
                raise CommandError(f"Expected per-100 bond quote: {security.symbol}")
            if row["asset_category_label"] == "基金类" and security.asset_type not in {Security.TYPE_ETF, Security.TYPE_FUND}:
                raise CommandError(f"Expected fund/ETF security: {security.symbol}")
            result[key] = security
        return result

    @staticmethod
    def _validate_positions(records, accounts, securities):
        grouped = defaultdict(list)
        external_ids = [row["external_id"] for row in records]
        for row in records:
            grouped[(row["account_name"], row["market"], row["symbol"])].append(row)
        for (account_name, market, symbol), rows in grouped.items():
            account = accounts[account_name]
            security = securities[(market, symbol)]
            events = []
            existing = InvestmentTransaction.objects.filter(
                account=account,
                security=security,
                status__in=[TradeStatusChoices.PARTIAL, TradeStatusChoices.COMPLETED],
            ).exclude(
                source=TransactionSourceChoices.IMPORT,
                external_id__in=external_ids,
            ).order_by("trade_date", "created_at", "pk")
            for item in existing:
                delta = item.quantity if item.trade_type in {TradeTypeChoices.BUY, TradeTypeChoices.IPO} else -item.quantity
                if item.trade_type in {TradeTypeChoices.BUY, TradeTypeChoices.IPO, TradeTypeChoices.SELL}:
                    events.append((item.trade_date, 0, item.pk, delta))
            for row in rows:
                delta = row["quantity"] if row["trade_type"] == TradeTypeChoices.BUY else -row["quantity"]
                events.append((row["trade_date"], 1, row["source_row"], delta))
            balance = Decimal("0")
            for trade_date, _, marker, delta in sorted(events):
                balance += delta
                if balance < 0:
                    raise CommandError(
                        f"Position would be negative: {account_name} {symbol} on {trade_date} at {marker}"
                    )
