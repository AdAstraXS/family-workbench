from datetime import timedelta

from ipo.models import HkIpoListing
from ledger.models import BankAccount

from .import_ipo_trades_2026 import (
    Command as Import2026Command,
    money,
    normalized_name,
)


ACCOUNT_NAMES = (
    "老虎证券",
    "粤商证券1 xiao 719056",
    "粤商证券2 xiao 719800",
    "信诚MP5418",
    "粤商证券 feng 719775",
    "粤商证券 long 719777",
    "熊猫证券-张",
    "富途证券",
    "信诚MP5417",
)

LISTING_ALIASES = {
    "佳鑫国际": "佳鑫國際",
    "劲方医药": "劲方医药-Ｂ",
    "紫金矿业": "紫金黄金国际",
    "八马茶业": "八马茶叶",
    "明略科技-W": "明略科技",
    "宝济药业": "宝济药业-B",
    "51视界": "五一视界",
    "药捷安康": "药捷安康-B",
    "绿茶": "绿茶集团",
}


class Command(Import2026Command):
    help = "Import 2025 HK IPO subscription and sale records from the audited workbook payload."

    def add_arguments(self, parser):
        parser.add_argument(
            "json_path",
            nargs="?",
            default="/app/tmp/ipo_trades_2025.json",
        )
        parser.add_argument("--commit", action="store_true")

    def load_accounts(self, member):
        return {
            name: BankAccount.objects.get(
                member=member,
                account_name=name,
                is_active=True,
            )
            for name in ACCOUNT_NAMES
        }

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
        normalized_row = dict(row)
        normalized_row["excel_final_price"] = (
            row.get("excel_final_price") or listing.final_price
        )
        normalized_row["excel_lot_size"] = row.get("excel_lot_size") or listing.lot_size
        item = super().prepare_record(normalized_row, listing, member, account)

        item["sell_date"] = (
            listing.allotment_result_date
            or (
                listing.subscription_end_date + timedelta(days=2)
                if listing.subscription_end_date
                else None
            )
        )
        if item["sell_date"] is None:
            raise ValueError(
                f"{listing.stock_name} has neither an allotment result date "
                "nor a subscription end date"
            )

        item["extra_data"].update(
            {
                "source_file": "2025-港股打新统计.xlsx",
                "source_sheet": "账户统计2025",
                "source_column": row.get("source_column"),
                "source_workbook_fees_total": row.get(
                    "source_workbook_fees_total", row.get("fees_total")
                ),
                "source_workbook_net_profit": row.get(
                    "source_workbook_net_profit", row.get("net_profit")
                ),
                "sell_date_basis": (
                    "源表未提供卖出日期；统一按结果公布日，缺失时按招股截止日后2日。"
                ),
            }
        )
        item["source_row"] = normalized_row
        return item
