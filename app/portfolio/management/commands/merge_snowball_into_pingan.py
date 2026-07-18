from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from ledger.models import AssetBalanceEntry
from portfolio.models import InvestmentAccount


class Command(BaseCommand):
    help = "将孙秘书雪球账户的家庭账本历史余额合并到平安证券并删除空账户。"

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="正式写入。")

    def handle(self, *args, **options):
        self.stdout.write(
            "将2024-12-31雪球账户16,869 CNY明细改挂平安证券，并删除雪球账户。"
        )
        if not options["apply"]:
            self.stdout.write(self.style.WARNING("预览完成；数据库未修改。"))
            return

        with transaction.atomic():
            snowball = (
                InvestmentAccount.objects.select_for_update()
                .select_related("bank_account__member")
                .get(pk=28)
            )
            pingan = (
                InvestmentAccount.objects.select_for_update()
                .select_related("bank_account__member")
                .get(pk=3)
            )
            if (
                snowball.member.display_name != "孙秘书"
                or snowball.account_name != "雪球"
                or pingan.member_id != snowball.member_id
                or pingan.account_name != "平安证券"
            ):
                raise CommandError("雪球或平安证券账户身份与预期不符。")
            if any(
                (
                    snowball.transactions.exists(),
                    snowball.cash_movements.exists(),
                    snowball.positions.exists(),
                    snowball.snapshots.exists(),
                    snowball.snapshot_position_lines.exists(),
                    snowball.balance_anchors.exists(),
                )
            ):
                raise CommandError("雪球投资账户仍有其他关联数据，已中止。")

            source_bank = snowball.bank_account
            entries = list(
                AssetBalanceEntry.objects.select_for_update().filter(
                    account=source_bank
                ).order_by("pk")
            )
            if len(entries) != 1:
                raise CommandError(f"雪球账本明细应为1条，实际为{len(entries)}条。")
            entry = entries[0]
            if (
                entry.snapshot.snapshot_date.isoformat() != "2024-12-31"
                or entry.currency != "CNY"
                or entry.original_amount != 16869
                or entry.base_amount != 16869
            ):
                raise CommandError("雪球历史余额与预期不符，已中止。")

            entry.account = pingan.bank_account
            entry.account_name = pingan.account_name
            entry.remark = "\n".join(
                filter(None, [entry.remark, "原雪球账户余额合并至平安证券"])
            )
            entry.save(update_fields=["account", "account_name", "remark", "updated_at"])
            snowball.delete()
            source_bank.delete()

        self.stdout.write(self.style.SUCCESS("雪球余额已合并，雪球账户已删除。"))
