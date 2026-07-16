import json
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from family_core.models import FamilyMember
from ledger.models import BankAccount
from portfolio.ipo_sync import (
    _portfolio_account,
    _security,
    delete_synced_ipo_transactions,
    refresh_ipo_sale_summary,
    sync_ipo_trade,
)
from portfolio.models import (
    InvestmentOption,
    InvestmentTransaction,
    TradeStatusChoices,
    TradeTypeChoices,
    TransactionSourceChoices,
)
from portfolio.services import rebuild_position

from ...models import HkIpoListing, HkIpoSubscriptionTrade


STEP = Decimal("0.0001")


def money(value):
    return Decimal(str(value or 0)).quantize(STEP, rounding=ROUND_HALF_UP)


def parsed_date(value):
    return date.fromisoformat(value) if value else None


def normalized_code(value):
    return str(value or "").upper().removesuffix(".HK").zfill(5)


class Command(BaseCommand):
    help = "Import grouped 2025-2026 IPO subscriptions and sale lots for 孙秘书."

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

        member = FamilyMember.objects.get(display_name="孙秘书", is_active=True)
        accounts = {
            item.account_name: item
            for item in BankAccount.objects.filter(
                member=member,
                is_active=True,
                supports_investment=True,
            )
        }
        listings = {
            normalized_code(item.stock_code): item
            for item in HkIpoListing.objects.all()
        }
        errors = []
        warnings = []
        new_listings = []
        existing = 0
        sale_count = 0
        unallotted_fee_count = 0

        for row in records:
            code = normalized_code(row["stock_code"])
            account = accounts.get(row["account"])
            if not account:
                errors.append(f"{code}: account not found: {row['account']}")
                continue
            listing = listings.get(code)
            if listing is None:
                if not row.get("final_price") or not row.get("lot_size"):
                    errors.append(f"{code}: missing listing and source price/lot size")
                    continue
                new_listings.append(code)
            else:
                if row.get("lot_size") and listing.lot_size != int(row["lot_size"]):
                    errors.append(
                        f"{code}: lot size DB={listing.lot_size} source={row['lot_size']}"
                    )
                source_price = money(row.get("final_price"))
                if source_price and listing.final_price != source_price:
                    warnings.append(
                        f"{code}: keep DB final price {listing.final_price}; source={source_price}"
                    )
                source_key = self.source_key(row)
                existing_trade = HkIpoSubscriptionTrade.objects.filter(
                    member=member,
                    account=account,
                    listing=listing,
                    application_date=parsed_date(row["application_date"]),
                ).first()
                if existing_trade and (existing_trade.extra_data or {}).get("source_key") != source_key:
                    errors.append(
                        f"{code}/{row['account']}: existing non-import trade #{existing_trade.pk}"
                    )
                elif existing_trade:
                    existing += 1
            sale_count += len(row.get("sales") or [])
            unallotted_fee_count += int(
                int(row["allotted_lots"]) == 0 and money(row.get("subscription_fee")) > 0
            )

        if errors:
            raise CommandError("\n".join(errors))

        self.stdout.write(
            f"mode={'COMMIT' if options['commit'] else 'DRY-RUN'} "
            f"subscriptions={len(records)} sales={sale_count} "
            f"unallotted_fees={unallotted_fee_count} "
            f"new_listings={len(set(new_listings))} existing_imports={existing}"
        )
        for line in warnings:
            self.stdout.write(self.style.WARNING(line))
        if not options["commit"]:
            self.stdout.write(self.style.WARNING("Dry run only; use --commit to write."))
            return

        with transaction.atomic():
            created_listings = 0
            created_trades = 0
            updated_trades = 0
            created_sales = 0
            for row in records:
                code = normalized_code(row["stock_code"])
                listing = listings.get(code)
                if listing is None:
                    first_sale_date = min(
                        (parsed_date(item["sale_date"]) for item in row.get("sales") or []),
                        default=None,
                    )
                    listing = HkIpoListing.objects.create(
                        stock_code=code,
                        stock_name=row["stock_name"],
                        company_name=row["stock_name"],
                        subscription_end_date=parsed_date(row["application_date"]),
                        allotment_result_date=first_sale_date,
                        listing_date=first_sale_date,
                        final_price=money(row["final_price"]),
                        lot_size=int(row["lot_size"]),
                        extra_data={
                            "source_file": payload["source"],
                            "source_sheet": payload["sheet"],
                        },
                    )
                    listings[code] = listing
                    created_listings += 1

                account = accounts[row["account"]]
                source_key = self.source_key(row)
                trade = HkIpoSubscriptionTrade.objects.filter(
                    extra_data__source_key=source_key
                ).first()
                if trade:
                    delete_synced_ipo_transactions(trade.pk)
                    updated_trades += 1
                else:
                    trade = HkIpoSubscriptionTrade()
                    created_trades += 1

                trade.listing = listing
                trade.member = member
                trade.account = account
                trade.application_date = parsed_date(row["application_date"])
                trade.tranche = HkIpoSubscriptionTrade.TRANCHE_MID_A
                trade.applied_lots = max(int(row["applied_lots"]), 1)
                trade.application_method = HkIpoSubscriptionTrade.METHOD_MARGIN
                trade.financing_interest = Decimal("0")
                trade.subscription_fee = money(row.get("subscription_fee"))
                trade.allotted_lots = int(row["allotted_lots"])
                trade.sold_lots = 0
                trade.sell_date = None
                trade.sell_price = Decimal("0")
                trade.trading_fee = Decimal("0")
                trade.realized_profit = Decimal("0")
                trade.remark = "源表未提供申购手数；按中签手数（未中签按1手）占位。"
                trade.extra_data = {
                    **(trade.extra_data or {}),
                    "source_key": source_key,
                    "source_file": payload["source"],
                    "source_sheet": payload["sheet"],
                    "source_rows": row["source_rows"],
                    "source_applied_lots_missing": True,
                }
                trade.save()
                sync_ipo_trade(trade.pk)

                sales = row.get("sales") or []
                if sales:
                    portfolio_account = _portfolio_account(trade)
                    security = _security(trade)
                    sell_option = InvestmentOption.objects.filter(
                        category=InvestmentOption.CATEGORY_TRANSACTION_TYPE,
                        code=TradeTypeChoices.SELL,
                        is_active=True,
                    ).first()
                    for sale in sales:
                        quantity = Decimal(int(sale["sold_shares"]))
                        price = money(sale["price"])
                        InvestmentTransaction.objects.create(
                            account=portfolio_account,
                            ipo_subscription_trade=trade,
                            security=security,
                            asset_category=security.asset_category,
                            trade_date=parsed_date(sale["sale_date"]),
                            trade_type=TradeTypeChoices.SELL,
                            trade_type_option=sell_option,
                            status=TradeStatusChoices.COMPLETED,
                            quantity=quantity,
                            price=price,
                            amount=quantity * price,
                            fee=money(sale.get("fee")),
                            tax=Decimal("0"),
                            currency=security.currency,
                            source=TransactionSourceChoices.IPO,
                            external_id=f"ipo:{trade.pk}:sell:source-row-{sale['source_row']}",
                            extra_data={
                                "historical_workbook_import": True,
                                "source_row": sale["source_row"],
                            },
                        )
                        created_sales += 1
                    rebuild_position(portfolio_account, security)
                    refresh_ipo_sale_summary(trade)
        self.stdout.write(
            self.style.SUCCESS(
                f"Imported listings={created_listings}, subscriptions={created_trades}, "
                f"updated={updated_trades}, sales={created_sales}."
            )
        )

    @staticmethod
    def source_key(row):
        return f"secretary-history:{row['account']}:{row['stock_code']}:{row['application_date']}"
