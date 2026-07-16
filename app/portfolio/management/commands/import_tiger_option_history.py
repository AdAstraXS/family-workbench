import json
from collections import defaultdict
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from portfolio.models import (
    InvestmentAccount,
    InvestmentTransaction,
    OptionContract,
    Security,
    TradeStatusChoices,
    TradeTypeChoices,
    TransactionSourceChoices,
)
from portfolio.services import rebuild_position


MONEY_STEP = Decimal("0.0001")
IMPORT_PREFIX = "tiger-option-history"


def money(value):
    return Decimal(str(value or 0)).quantize(MONEY_STEP, rounding=ROUND_HALF_UP)


def allocate_group_fees(records, fee_groups):
    allocated = {}
    grouped = defaultdict(list)
    for row in records:
        grouped[row["underlying_symbol"]].append(row)
    for symbol, rows in grouped.items():
        total_fee = money(fee_groups.get(symbol))
        total_quantity = sum((Decimal(row["quantity"]) for row in rows), Decimal("0"))
        used = Decimal("0")
        for index, row in enumerate(rows):
            fee = (
                total_fee - used
                if index == len(rows) - 1
                else money(total_fee * Decimal(row["quantity"]) / total_quantity)
            )
            allocated[row["source_row"]] = fee
            used += fee
    return allocated


def remaining_positions(records):
    positions = defaultdict(Decimal)
    for row in records:
        quantity = Decimal(row["quantity"])
        positions[row["contract_symbol"]] += (
            quantity if row["trade_type"] == TradeTypeChoices.BUY else -quantity
        )
    return {symbol: quantity for symbol, quantity in positions.items() if quantity}


def positions_due_for_expiry(records, through_date=None):
    remaining = remaining_positions(records)
    if not through_date:
        return remaining
    cutoff = date.fromisoformat(through_date)
    expirations = {
        row["contract_symbol"]: date.fromisoformat(row["expiration_date"])
        for row in records
    }
    return {
        symbol: quantity
        for symbol, quantity in remaining.items()
        if expirations[symbol] <= cutoff
    }


class Command(BaseCommand):
    help = "Import 2025-2026 Tiger option trades and zero-value expiry closes."

    def add_arguments(self, parser):
        parser.add_argument("json_path")
        parser.add_argument("--commit", action="store_true")

    def handle(self, *args, **options):
        path = Path(options["json_path"])
        if not path.exists():
            raise CommandError(f"Import file not found: {path}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        records = payload.get("records") or []
        if not records:
            raise CommandError("No records found.")

        account = self._account(payload)
        import_prefix = payload.get("import_prefix", IMPORT_PREFIX)
        import_remark = payload.get("remark", "老虎证券期权历史导入")
        fees = allocate_group_fees(records, payload.get("fee_groups") or {})
        remaining = positions_due_for_expiry(
            records, payload.get("expiry_close_through")
        )
        contracts = {row["contract_symbol"] for row in records}
        source_rows = [row["source_row"] for row in records]
        if len(source_rows) != len(set(source_rows)):
            raise CommandError("Duplicate source rows found.")
        if any(int(row["multiplier"]) != 100 for row in records):
            raise CommandError("Only 100-share option contracts are supported by this import.")
        if any(row["currency"] != "USD" for row in records):
            raise CommandError("Only USD rows are expected in this import.")

        self.stdout.write(
            f"mode={'COMMIT' if options['commit'] else 'DRY-RUN'} account={account.pk} "
            f"rows={len(records)} contracts={len(contracts)} expiry_closes={len(remaining)} "
            f"fees={sum(fees.values(), Decimal('0')):.4f}"
        )
        for symbol in sorted(payload.get("fee_groups") or {}):
            count = sum(Decimal(row["quantity"]) for row in records if row["underlying_symbol"] == symbol)
            self.stdout.write(
                f"fee_group {symbol}: contracts={count} total={sum(fees[row['source_row']] for row in records if row['underlying_symbol'] == symbol):.4f}"
            )
        if not options["commit"]:
            self.stdout.write(self.style.WARNING("Dry run only; use --commit to write."))
            return

        with transaction.atomic():
            securities = {}
            created_underlyings = 0
            created_contracts = 0
            created_transactions = 0
            updated_transactions = 0
            for row in records:
                contract = row["contract_symbol"]
                if contract not in securities:
                    security, underlying_created, contract_created = self._security(
                        account, row, payload
                    )
                    securities[contract] = security
                    created_underlyings += int(underlying_created)
                    created_contracts += int(contract_created)
                _, created = InvestmentTransaction.objects.update_or_create(
                    account=account,
                    source=TransactionSourceChoices.IMPORT,
                    external_id=f"{import_prefix}:row-{row['source_row']}",
                    defaults={
                        "security": securities[contract],
                        "asset_category": securities[contract].asset_category,
                        "trade_date": date.fromisoformat(row["trade_date"]),
                        "trade_type": row["trade_type"],
                        "position_effect": row["position_effect"],
                        "status": TradeStatusChoices.COMPLETED,
                        "quantity": Decimal(row["quantity"]),
                        "price": Decimal(row["price"]),
                        "amount": money(
                            Decimal(row["quantity"])
                            * Decimal(row["price"])
                            * Decimal(row["multiplier"])
                        ),
                        "fee": fees[row["source_row"]],
                        "tax": Decimal("0"),
                        "currency": row["currency"],
                        "remark": import_remark,
                        "extra_data": {
                            "historical_workbook_import": True,
                            "source_file": payload["source"],
                            "source_sheet": payload["sheet"],
                            "source_row": row["source_row"],
                            "workbook_realized_pnl": row.get("workbook_realized_pnl"),
                        },
                    },
                )
                created_transactions += int(created)
                updated_transactions += int(not created)

            rows_by_contract = {row["contract_symbol"]: row for row in records}
            assignment_links = 0
            for contract, signed_quantity in remaining.items():
                row = rows_by_contract[contract]
                security = securities[contract]
                expiry_trade, created = InvestmentTransaction.objects.update_or_create(
                    account=account,
                    source=TransactionSourceChoices.IMPORT,
                    external_id=f"{import_prefix}:expiry:{contract}",
                    defaults={
                        "security": security,
                        "asset_category": security.asset_category,
                        "trade_date": date.fromisoformat(row["expiration_date"]),
                        "trade_type": (
                            TradeTypeChoices.SELL
                            if signed_quantity > 0
                            else TradeTypeChoices.BUY
                        ),
                        "position_effect": InvestmentTransaction.EFFECT_CLOSE,
                        "status": TradeStatusChoices.COMPLETED,
                        "quantity": abs(signed_quantity),
                        "price": Decimal("0"),
                        "amount": Decimal("0"),
                        "fee": Decimal("0"),
                        "tax": Decimal("0"),
                        "currency": row["currency"],
                        "remark": "到期作废（零价值自动平仓）",
                        "extra_data": {
                            "historical_workbook_import": True,
                            "expiry_reconciliation": True,
                            "source_file": payload["source"],
                        },
                    },
                )
                created_transactions += int(created)
                updated_transactions += int(not created)
                assignment_links += int(
                    self._link_underlying_settlement(
                        account, expiry_trade, signed_quantity
                    )
                )

            for security in securities.values():
                rebuild_position(account, security)

        self.stdout.write(
            self.style.SUCCESS(
                f"Imported underlyings={created_underlyings}, contracts={created_contracts}, "
                f"transactions={created_transactions}, updated={updated_transactions}, "
                f"assignment_links={assignment_links}."
            )
        )

    @staticmethod
    def _link_underlying_settlement(account, expiry_trade, signed_quantity):
        option = expiry_trade.security.option_contract
        is_buy = (option.option_type == OptionContract.PUT) == (signed_quantity < 0)
        matches = InvestmentTransaction.objects.filter(
            account=account,
            security=option.underlying,
            trade_date=option.expiration_date,
            trade_type=TradeTypeChoices.BUY if is_buy else TradeTypeChoices.SELL,
            quantity=abs(signed_quantity) * option.multiplier,
            price=option.strike_price,
        )
        if matches.count() > 1:
            raise CommandError(f"Ambiguous underlying settlement: {expiry_trade.security.symbol}")
        underlying_trade = matches.first()
        if not underlying_trade:
            return False

        resolution = "assignment" if signed_quantity < 0 else "exercise"
        label = "被指派" if resolution == "assignment" else "行权"
        expiry_trade.remark = f"到期{label}，期权合约归零"
        expiry_trade.extra_data = {
            **(expiry_trade.extra_data or {}),
            "expiry_resolution": resolution,
            "linked_underlying_transaction_id": underlying_trade.pk,
        }
        expiry_trade.save(update_fields=["remark", "extra_data", "updated_at"])

        note = f"由 {expiry_trade.security.symbol} 到期{label}产生"
        if note not in (underlying_trade.remark or ""):
            underlying_trade.remark = "；".join(filter(None, [underlying_trade.remark, note]))
        underlying_trade.extra_data = {
            **(underlying_trade.extra_data or {}),
            "option_expiry_resolution": resolution,
            "linked_option_transaction_id": expiry_trade.pk,
            "option_contract_symbol": expiry_trade.security.symbol,
        }
        underlying_trade.save(update_fields=["remark", "extra_data", "updated_at"])
        return True

    @staticmethod
    def _account(payload):
        try:
            return InvestmentAccount.objects.select_related(
                "bank_account__member__family"
            ).get(
                bank_account__member__display_name=payload["member"],
                bank_account__account_name=payload["account"],
                bank_account__is_active=True,
            )
        except InvestmentAccount.DoesNotExist as exc:
            raise CommandError(
                f"Investment account not found: {payload['member']} / {payload['account']}"
            ) from exc

    @staticmethod
    def _security(account, row, payload):
        family = account.family
        underlying_asset_type = row.get("underlying_asset_type", Security.TYPE_STOCK)
        underlying, underlying_created = Security.objects.get_or_create(
            symbol=row["underlying_symbol"],
            market="US",
            defaults={
                "asset_category": Security.default_asset_category(family, underlying_asset_type),
                "name": row["underlying_name"],
                "asset_type": underlying_asset_type,
                "currency": "USD",
                "data_source": "manual",
            },
        )
        if underlying.asset_type == Security.TYPE_OPTION:
            raise CommandError(f"Underlying is an option: {underlying.symbol}")
        if underlying.asset_type != underlying_asset_type:
            raise CommandError(
                f"Underlying type mismatch: {underlying.symbol} "
                f"{underlying.asset_type} != {underlying_asset_type}"
            )

        option_type = row["option_type"]
        strike = Decimal(row["strike_price"])
        expiration = date.fromisoformat(row["expiration_date"])
        existing = OptionContract.objects.select_related("security").filter(
            underlying=underlying,
            option_type=option_type,
            strike_price=strike,
            expiration_date=expiration,
        ).first()
        if existing:
            return existing.security, underlying_created, False

        security, security_created = Security.objects.get_or_create(
            symbol=row["contract_symbol"],
            market="US",
            defaults={
                "asset_category": Security.default_asset_category(family, Security.TYPE_OPTION),
                "name": f"{underlying.name} {expiration} {option_type.upper()} {strike}",
                "asset_type": Security.TYPE_OPTION,
                "currency": "USD",
                "data_source": "import",
                "extra_data": {"source_file": payload["source"]},
            },
        )
        if not security_created and security.asset_type != Security.TYPE_OPTION:
            raise CommandError(f"Contract symbol conflicts with non-option: {security.symbol}")
        OptionContract.objects.create(
            security=security,
            underlying=underlying,
            option_type=option_type,
            strike_price=strike,
            expiration_date=expiration,
            multiplier=int(row["multiplier"]),
        )
        return security, underlying_created, True
