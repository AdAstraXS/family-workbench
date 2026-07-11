import json
import re
import unicodedata
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from family_core.models import FamilyMember
from ipo.models import HkIpoListing, HkIpoSubscriptionTrade
from ledger.models import BankAccount


MONEY_STEP = Decimal("0.0001")

ACCOUNT_ALIASES = {
    "老虎": "老虎证券",
    "富途": "富途证券",
    "信诚证券MP5418": "信诚MP5418",
    "信诚证券NJ1122": "信诚NJ1122",
    "熊猫证券-张": "熊猫证券-张",
    "致富证券（公司户）": "致富证券（公户）",
}

LISTING_ALIASES = {
    "MINIMAX": "MINIMAX-WP",
    "BBSB": "BBSB INTL",
    "海致科技集团": "海致科技",
    "德适": "德适－B",
    "曦智科技": "曦智科技－P",
    "商米科技": "商米科技-W",
    "拓扑数控": "拓璞数控",
}


def money(value):
    if value in (None, ""):
        return Decimal("0")
    return Decimal(str(value)).quantize(MONEY_STEP, rounding=ROUND_HALF_UP)


def normalized_name(value):
    return (
        unicodedata.normalize("NFKC", str(value or ""))
        .strip()
        .upper()
        .replace("—", "-")
        .replace("–", "-")
        .replace("－", "-")
        .replace("（", "(")
        .replace("）", ")")
        .replace(" ", "")
    )


def excel_date(value):
    if isinstance(value, (int, float)):
        return date(1899, 12, 30) + timedelta(days=int(value))
    return None


class Command(BaseCommand):
    help = "Import 2026 HK IPO subscription and sale records extracted from the user's workbook."

    def add_arguments(self, parser):
        parser.add_argument(
            "json_path",
            nargs="?",
            default="/app/tmp/ipo_trades_2026.json",
        )
        parser.add_argument("--commit", action="store_true")

    def handle(self, *args, **options):
        source_path = Path(options["json_path"])
        if not source_path.exists():
            raise CommandError(f"Import file not found: {source_path}")

        payload = json.loads(source_path.read_text(encoding="utf-8"))
        records = payload.get("records") or []
        if not records:
            raise CommandError("No records found in import file.")

        member = FamilyMember.objects.get(display_name="我")
        accounts = self.load_accounts(member)
        listings = list(HkIpoListing.objects.all())

        prepared = []
        listing_updates = {}
        errors = []

        for index, row in enumerate(records, start=1):
            try:
                listing = self.find_listing(listings, row)
                account = accounts[row["broker"]]
                prepared.append(self.prepare_record(row, listing, member, account))

                final_price = money(row.get("excel_final_price"))
                lot_size = int(row.get("excel_lot_size") or 0)
                if final_price > 0 and lot_size > 0:
                    listing_updates[listing.pk] = (listing, final_price, lot_size)
            except (KeyError, ValueError, HkIpoListing.DoesNotExist) as exc:
                errors.append(f"Row {index}: {row.get('listing_name')} / {row.get('broker')}: {exc}")

        if errors:
            raise CommandError("\n".join(errors))

        self.stdout.write(
            f"Prepared {len(prepared)} trades for {len(listing_updates)} listings "
            f"from {payload.get('source', source_path.name)}."
        )

        price_changes = []
        for listing, final_price, lot_size in listing_updates.values():
            if listing.final_price != final_price or listing.lot_size != lot_size:
                price_changes.append(
                    f"{listing.stock_code} {listing.stock_name}: "
                    f"{listing.final_price}/{listing.lot_size} -> {final_price}/{lot_size}"
                )

        if price_changes:
            self.stdout.write("Listing value corrections:")
            for line in price_changes:
                self.stdout.write(f"  {line}")

        if not options["commit"]:
            self.stdout.write(self.style.WARNING("Dry run only. Re-run with --commit to write data."))
            return

        created = 0
        updated = 0
        precision_adjustments = []
        mismatches = []

        with transaction.atomic():
            for listing, final_price, lot_size in listing_updates.values():
                listing.final_price = final_price
                listing.lot_size = lot_size
                listing.save()

            for item in prepared:
                row = item.pop("source_row")
                trade, was_created = HkIpoSubscriptionTrade.objects.update_or_create(
                    listing=item.pop("listing"),
                    member=item.pop("member"),
                    account=item.pop("account"),
                    defaults=item,
                )
                created += int(was_created)
                updated += int(not was_created)

                expected = money(row.get("net_profit"))
                difference = (trade.realized_profit - expected).copy_abs()
                if Decimal("0.02") < difference <= Decimal("0.10"):
                    HkIpoSubscriptionTrade.objects.filter(pk=trade.pk).update(
                        realized_profit=expected
                    )
                    precision_adjustments.append(
                        f"{trade.listing.stock_name}/{trade.account.account_name}: "
                        f"{trade.realized_profit} -> {expected}"
                    )
                elif difference > Decimal("0.10"):
                    mismatches.append(
                        f"{trade.listing.stock_name}/{trade.account.account_name}: "
                        f"DB {trade.realized_profit} vs Excel {expected}"
                    )

        self.stdout.write(self.style.SUCCESS(f"Imported: {created} created, {updated} updated."))
        if precision_adjustments:
            self.stdout.write(
                f"Applied {len(precision_adjustments)} source-value precision adjustment(s):"
            )
            for line in precision_adjustments:
                self.stdout.write(f"  {line}")
        if mismatches:
            self.stdout.write(self.style.WARNING(f"Net profit mismatches: {len(mismatches)}"))
            for line in mismatches:
                self.stdout.write(f"  {line}")
        else:
            self.stdout.write(self.style.SUCCESS("All imported net profits match the workbook."))

    def load_accounts(self, member):
        result = {}
        for workbook_name, account_name in ACCOUNT_ALIASES.items():
            result[workbook_name] = BankAccount.objects.get(
                member=member,
                account_name=account_name,
                is_active=True,
            )
        return result

    def find_listing(self, listings, row):
        source_name = row["listing_name"]
        target_name = LISTING_ALIASES.get(source_name, source_name)
        target = normalized_name(target_name)
        final_price = money(row.get("excel_final_price"))
        lot_size = int(row.get("excel_lot_size") or 0)

        matches = [
            listing
            for listing in listings
            if target in normalized_name(listing.stock_name)
            or normalized_name(listing.stock_name) in target
        ]
        if len(matches) > 1:
            exact_values = [
                listing
                for listing in matches
                if listing.final_price == final_price and listing.lot_size == lot_size
            ]
            if len(exact_values) == 1:
                return exact_values[0]
        if len(matches) != 1:
            raise HkIpoListing.DoesNotExist(
                f"expected one listing match for {source_name!r}, found {len(matches)}"
            )
        return matches[0]

    def prepare_record(self, row, listing, member, account):
        application = str(row.get("application") or "").strip()
        allotted_lots = int(row.get("allotted_lots") or 0)
        sell_price = money(row.get("sell_price"))
        sold_lots = allotted_lots if sell_price > 0 else 0
        fees_total = money(row.get("fees_total"))
        allotment_fee = (
            Decimal(allotted_lots)
            * money(row.get("excel_final_price"))
            * Decimal(int(row.get("excel_lot_size") or 0))
            * Decimal("0.01")
        ).quantize(MONEY_STEP, rounding=ROUND_HALF_UP)
        subscription_fee = (fees_total - allotment_fee).quantize(
            MONEY_STEP, rounding=ROUND_HALF_UP
        )
        if subscription_fee < 0:
            raise ValueError(
                f"fees {fees_total} are less than the 1% allotment fee {allotment_fee}"
            )

        application_date = excel_date(row.get("period")) or listing.subscription_start_date
        if application_date is None:
            application_date = listing.subscription_end_date or listing.listing_date

        applied_lots = self.parse_applied_lots(application, allotted_lots)
        application_method = (
            HkIpoSubscriptionTrade.METHOD_CASH
            if "现金" in application
            else HkIpoSubscriptionTrade.METHOD_MARGIN
        )

        remark_parts = []
        if application:
            remark_parts.append(f"Excel认购：{application}")
        if row.get("manual_note"):
            remark_parts.append(row["manual_note"])

        return {
            "listing": listing,
            "member": member,
            "account": account,
            "application_date": application_date,
            "tranche": self.parse_tranche(application),
            "applied_lots": applied_lots,
            "application_method": application_method,
            "financing_interest": Decimal("0"),
            "subscription_fee": subscription_fee,
            "allotted_lots": allotted_lots,
            "sell_price": sell_price,
            "sold_lots": sold_lots,
            "trading_fee": Decimal("0"),
            "remark": "；".join(remark_parts),
            "extra_data": {
                "source_file": "2026-港股打新统计.xlsx",
                "source_sheet": "账户统计2026",
                "source_excel_row": row.get("excel_row"),
                "source_period": row.get("period"),
                "source_application": application,
                "source_gross_profit": row.get("gross_profit"),
                "source_fees_total": row.get("fees_total"),
                "source_net_profit": row.get("net_profit"),
            },
            "source_row": row,
        }

    def parse_applied_lots(self, application, allotted_lots):
        match = re.search(r"(\d+)\s*手", application)
        if match:
            return max(int(match.group(1)), allotted_lots, 1)
        return max(allotted_lots, 1)

    def parse_tranche(self, application):
        if "乙4" in application or "大乙" in application:
            return HkIpoSubscriptionTrade.TRANCHE_LARGE_B
        if "乙3" in application:
            return HkIpoSubscriptionTrade.TRANCHE_B3
        if "乙2" in application:
            return HkIpoSubscriptionTrade.TRANCHE_B2
        if "乙" in application:
            return HkIpoSubscriptionTrade.TRANCHE_HEAD_B
        if "甲尾" in application or "次甲尾" in application:
            return HkIpoSubscriptionTrade.TRANCHE_TAIL_A
        if "甲" in application:
            return HkIpoSubscriptionTrade.TRANCHE_MID_A
        return HkIpoSubscriptionTrade.TRANCHE_ONE_LOT
