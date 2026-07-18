from datetime import date
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Sum

from ledger.models import AssetBalanceEntry
from portfolio.models import (
    CashMovementTypeChoices,
    InvestmentAccount,
    InvestmentCashMovement,
    PortfolioAccountBalanceAnchor,
    TransactionSourceChoices,
)


CLEAR_BALANCES = {
    10: Decimal("45987.3400"),
    16: Decimal("16075.0000"),
    17: Decimal("13805.0000"),
    18: Decimal("7335.6000"),
    20: Decimal("19527.5000"),
    37: Decimal("11843.0140"),
}
ANCHOR_BANK_ACCOUNT_IDS = {72, 73, 74, 75, 79}


class Command(BaseCommand):
    help = "补录已确认的历史账户清零、期初余额及家庭账本余额锚点。"

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="正式写入；不传时只显示计划。",
        )

    def handle(self, *args, **options):
        self._show_plan()
        if not options["apply"]:
            self.stdout.write(self.style.WARNING("预览完成；数据库未修改。"))
            return

        with transaction.atomic():
            clear_count = self._create_clear_movements()
            opening_count = self._create_huili_opening_balance()
            anchor_count = self._create_ledger_anchors()

        self.stdout.write(
            self.style.SUCCESS(
                f"补录完成：清零出金{clear_count}条，期初余额{opening_count}条，"
                f"余额锚点{anchor_count}条。"
            )
        )

    def _show_plan(self):
        total = sum(CLEAR_BALANCES.values(), Decimal("0"))
        self.stdout.write(
            f"2025-12-30清零：5个粤商账户及信诚MP5417，共{total} HKD。"
        )
        self.stdout.write("辉立证券：2024-12-31补350 HKD历史期初余额调整。")
        self.stdout.write(
            "历史停用账户及无流水余额变化账户：按家庭账本原币建立独立余额锚点。"
        )

    def _create_clear_movements(self):
        created = 0
        for account_id, expected_balance in CLEAR_BALANCES.items():
            account = (
                InvestmentAccount.objects.select_for_update()
                .select_related("bank_account__member")
                .get(pk=account_id)
            )
            external_id = f"reconciliation-clear-{account_id}-20251230"
            if InvestmentCashMovement.objects.filter(
                account=account,
                source=TransactionSourceChoices.MANUAL,
                external_id=external_id,
            ).exists():
                continue
            if account.positions.exclude(quantity=0).exists():
                raise CommandError(f"{account}仍有持仓，不能只做现金清零。")
            InvestmentCashMovement.objects.create(
                account=account,
                movement_date=date(2025, 12, 30),
                settlement_date=date(2025, 12, 30),
                movement_type=CashMovementTypeChoices.WITHDRAWAL,
                currency="HKD",
                amount=-expected_balance,
                source=TransactionSourceChoices.MANUAL,
                external_id=external_id,
                remark=(
                    "历史账户清零：按用户确认，2025年交易结束后余额已全部转出；"
                    "原始出金流水缺失。"
                ),
            )
            created += 1
        return created

    def _create_huili_opening_balance(self):
        account = (
            InvestmentAccount.objects.select_for_update()
            .select_related("bank_account__member")
            .get(pk=15)
        )
        if account.member.display_name != "孙秘书" or account.account_name != "辉立证券":
            raise CommandError("辉立证券账户身份与预期不符。")
        external_id = "reconciliation-opening-15-20241231"
        if InvestmentCashMovement.objects.filter(
            account=account,
            source=TransactionSourceChoices.MANUAL,
            external_id=external_id,
        ).exists():
            return 0
        InvestmentCashMovement.objects.create(
            account=account,
            movement_date=date(2024, 12, 31),
            settlement_date=date(2024, 12, 31),
            movement_type=CashMovementTypeChoices.ADJUSTMENT,
            currency="HKD",
            amount=Decimal("350.0000"),
            source=TransactionSourceChoices.MANUAL,
            external_id=external_id,
            remark="历史期初余额调整：家庭账本各期原币余额均为350港币。",
        )
        return 1

    def _create_ledger_anchors(self):
        accounts = {
            account.bank_account_id: account
            for account in InvestmentAccount.objects.select_for_update()
            .select_related("bank_account")
            .filter(bank_account_id__in=ANCHOR_BANK_ACCOUNT_IDS)
        }
        if set(accounts) != ANCHOR_BANK_ACCOUNT_IDS:
            missing = sorted(ANCHOR_BANK_ACCOUNT_IDS - set(accounts))
            raise CommandError(f"缺少投资账户对应关系：{missing}")

        rows = (
            AssetBalanceEntry.objects.filter(account_id__in=ANCHOR_BANK_ACCOUNT_IDS)
            .values("snapshot_id", "snapshot__snapshot_date", "account_id", "currency")
            .annotate(
                original_amount=Sum("original_amount"),
                base_amount=Sum("base_amount"),
            )
            .order_by("snapshot__snapshot_date", "account_id", "currency")
        )
        created = 0
        for row in rows:
            account = accounts[row["account_id"]]
            is_historical = not account.is_active
            _, was_created = PortfolioAccountBalanceAnchor.objects.update_or_create(
                account=account,
                anchor_date=row["snapshot__snapshot_date"],
                currency=row["currency"],
                defaults={
                    "ledger_snapshot_id": row["snapshot_id"],
                    "original_amount": row["original_amount"],
                    "recorded_base_amount": row["base_amount"],
                    "reason": (
                        PortfolioAccountBalanceAnchor.REASON_HISTORICAL
                        if is_historical
                        else PortfolioAccountBalanceAnchor.REASON_RECONCILIATION
                    ),
                    "carry_forward": not is_historical,
                    "is_confirmed": True,
                    "remark": (
                        "历史停用账户仅用于历史展示，不补造交易流水。"
                        if is_historical
                        else "该账户无投资流水，快照以家庭账本原币余额为准。"
                    ),
                },
            )
            created += int(was_created)
        return created
