from decimal import Decimal, ROUND_HALF_UP

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from ledger.models import AssetBalanceEntry
from portfolio.models import InvestmentAccount


class Command(BaseCommand):
    help = "修正本地快照核对中已确认的账户归属和原币错误。"

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="正式写入；不传时仅显示将要执行的修正。",
        )

    def handle(self, *args, **options):
        self.stdout.write("将执行：NJ1122整户迁移、熊猫两期40万HKD调拨、盈透500原币改为HKD。")
        if not options["apply"]:
            self.stdout.write(self.style.WARNING("预览完成；数据库未修改。"))
            return

        with transaction.atomic():
            moved, merged = self._merge_nj1122()
            self._shift_panda_snapshots()
            self._correct_ibkr_currency()

        self.stdout.write(
            self.style.SUCCESS(
                f"修正完成：NJ1122迁移{moved}条、合并{merged}条；熊猫及盈透已修正。"
            )
        )

    def _merge_nj1122(self):
        source = (
            InvestmentAccount.objects.select_for_update()
            .select_related("bank_account__member")
            .get(pk=27)
        )
        target = (
            InvestmentAccount.objects.select_for_update()
            .select_related("bank_account__member")
            .get(pk=26)
        )
        if (
            source.account_name != "信诚NJ1122"
            or source.member.display_name != "我"
            or target.account_name != "信诚NJ1122"
            or target.member.display_name != "孙秘书"
        ):
            raise CommandError("NJ1122账户身份与预期不符，已中止。")
        if (
            source.transactions.exists()
            or source.cash_movements.exists()
            or source.positions.exists()
            or source.snapshots.exists()
            or source.snapshot_position_lines.exists()
        ):
            raise CommandError("我的NJ1122仍有投资组合关联数据，已中止。")

        source_bank = source.bank_account
        target_bank = target.bank_account
        target_member = target.member
        moved = merged = 0
        for entry in list(
            AssetBalanceEntry.objects.select_for_update()
            .filter(account=source_bank)
            .order_by("pk")
        ):
            match = (
                AssetBalanceEntry.objects.select_for_update()
                .filter(
                    snapshot=entry.snapshot,
                    member=target_member,
                    account=target_bank,
                    asset_category=entry.asset_category,
                    currency=entry.currency,
                )
                .exclude(pk=entry.pk)
                .first()
            )
            if match:
                match.original_amount += entry.original_amount
                match.base_amount += entry.base_amount
                match.remark = "\n".join(
                    filter(None, [match.remark, entry.remark, "由我的信诚NJ1122合并"])
                )
                match.save(
                    update_fields=["original_amount", "base_amount", "remark", "updated_at"]
                )
                entry.delete()
                merged += 1
            else:
                entry.account = target_bank
                entry.member = target_member
                entry.account_name = target_bank.account_name
                entry.remark = "\n".join(
                    filter(None, [entry.remark, "由我的信诚NJ1122迁移"])
                )
                entry.save(
                    update_fields=[
                        "account",
                        "member",
                        "account_name",
                        "remark",
                        "updated_at",
                    ]
                )
                moved += 1

        source.delete()
        source_bank.delete()
        return moved, merged

    def _shift_panda_snapshots(self):
        for snapshot_date in ("2025-12-31", "2026-01-31"):
            mine = (
                AssetBalanceEntry.objects.select_for_update()
                .select_related("snapshot")
                .get(
                    snapshot__snapshot_date=snapshot_date,
                    member__display_name="我",
                    account_id=45,
                    currency="HKD",
                )
            )
            sun = AssetBalanceEntry.objects.select_for_update().get(
                snapshot__snapshot_date=snapshot_date,
                member__display_name="孙秘书",
                account_id=46,
                currency="HKD",
            )
            shift = Decimal("400000.0000")
            if mine.original_amount < shift:
                raise CommandError(f"{snapshot_date} 我的熊猫证券余额不足40万港币。")
            base_shift = (shift * mine.snapshot.hkd_to_base).quantize(
                Decimal("0.0001"), rounding=ROUND_HALF_UP
            )
            mine.original_amount -= shift
            mine.base_amount -= base_shift
            sun.original_amount += shift
            sun.base_amount += base_shift
            mine.remark = "\n".join(
                filter(None, [mine.remark, "调拨40万港币至孙秘书熊猫证券"])
            )
            sun.remark = "\n".join(
                filter(None, [sun.remark, "由我的熊猫证券调入40万港币"])
            )
            mine.save(
                update_fields=["original_amount", "base_amount", "remark", "updated_at"]
            )
            sun.save(
                update_fields=["original_amount", "base_amount", "remark", "updated_at"]
            )

    def _correct_ibkr_currency(self):
        entry = (
            AssetBalanceEntry.objects.select_for_update()
            .select_related("snapshot", "member")
            .get(pk=379)
        )
        if (
            entry.member.display_name != "我"
            or entry.account_id != 30
            or entry.original_amount != Decimal("500.0000")
        ):
            raise CommandError("盈透证券500原币记录与预期不符，已中止。")
        entry.currency = "HKD"
        entry.base_amount = (entry.original_amount * entry.snapshot.hkd_to_base).quantize(
            Decimal("0.0001"), rounding=ROUND_HALF_UP
        )
        entry.remark = "\n".join(
            filter(None, [entry.remark, "原币由USD纠正为HKD"])
        )
        entry.save(
            update_fields=["currency", "base_amount", "remark", "updated_at"]
        )
