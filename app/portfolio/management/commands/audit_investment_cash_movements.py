from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from portfolio.models import (
    InvestmentCashMovement,
    InvestmentTransaction,
    TradeStatusChoices,
)
from portfolio.services import (
    TRADE_CASH_MOVEMENT_TYPES,
    rebuild_cash_only_transaction,
    rebuild_position,
)


ACTIVE_STATUSES = {TradeStatusChoices.PARTIAL, TradeStatusChoices.COMPLETED}


def cash_movement_issues():
    transactions = list(
        InvestmentTransaction.objects.select_related("account", "security").order_by("pk")
    )
    movements = {
        item.transaction_id: item
        for item in InvestmentCashMovement.objects.exclude(transaction=None)
    }
    issues = []
    for item in transactions:
        movement = movements.get(item.pk)
        should_exist = item.status in ACTIVE_STATUSES
        if should_exist and movement is None:
            issues.append((item, "缺失"))
            continue
        if not should_exist and movement is not None:
            issues.append((item, "多余"))
            continue
        if movement is None:
            continue
        expected = {
            "account_id": item.account_id,
            "movement_date": item.trade_date,
            "movement_type": TRADE_CASH_MOVEMENT_TYPES[item.trade_type],
            "currency": item.currency,
            "amount": item.cash_change,
            "source": item.source,
            "external_id": item.external_id,
        }
        mismatched = [
            field for field, value in expected.items() if getattr(movement, field) != value
        ]
        if mismatched:
            issues.append((item, f"不一致：{','.join(mismatched)}"))
    return transactions, movements, issues


class Command(BaseCommand):
    help = "审计投资交易与交易派生现金流水的一致性；默认只读，使用 --repair 才会重建。"

    def add_arguments(self, parser):
        parser.add_argument(
            "--repair",
            action="store_true",
            help="按投资交易记录重新生成相关现金流水。",
        )

    def handle(self, *args, **options):
        transactions, movements, issues = cash_movement_issues()
        counts = {
            "缺失": sum(reason == "缺失" for _, reason in issues),
            "多余": sum(reason == "多余" for _, reason in issues),
            "不一致": sum(reason.startswith("不一致") for _, reason in issues),
        }
        mode = "REPAIR" if options["repair"] else "AUDIT"
        self.stdout.write(
            f"模式={mode} 交易={len(transactions)} 交易派生流水={len(movements)} "
            f"缺失={counts['缺失']} 不一致={counts['不一致']} 多余={counts['多余']}"
        )
        for item, reason in issues[:50]:
            self.stdout.write(
                f"交易 #{item.pk} {item.transaction_no or ''} {item.trade_date}: {reason}"
            )
        if len(issues) > 50:
            self.stdout.write(f"另有 {len(issues) - 50} 项未展开。")
        if not issues:
            self.stdout.write(self.style.SUCCESS("交易与现金流水一致。"))
            return
        if not options["repair"]:
            self.stdout.write(
                self.style.WARNING("审计完成，未修改数据库；使用 --repair 才会重建。")
            )
            return

        with transaction.atomic():
            pairs = {
                (item.account_id, item.security_id)
                for item, _ in issues
                if item.security_id
            }
            cash_only_ids = {
                item.pk for item, _ in issues if not item.security_id
            }
            for account_id, security_id in pairs:
                item = (
                    InvestmentTransaction.objects.select_related("account", "security")
                    .filter(account_id=account_id, security_id=security_id)
                    .first()
                )
                rebuild_position(item.account, item.security)
            for item in InvestmentTransaction.objects.filter(pk__in=cash_only_ids):
                rebuild_cash_only_transaction(item)

            _, _, remaining = cash_movement_issues()
            if remaining:
                raise CommandError(
                    f"修复后仍有 {len(remaining)} 项不一致，数据库修改已回滚。"
                )
        self.stdout.write(
            self.style.SUCCESS(
                f"修复完成：重建持仓组 {len(pairs)}，现金类交易 {len(cash_only_ids)}。"
            )
        )
