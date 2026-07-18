from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from ipo.date_rules import MISSING_IPO_ACCOUNTING_DATE_MESSAGE, ipo_accounting_date
from ipo.models import HkIpoSubscriptionTrade
from portfolio.ipo_sync import _security_identity, sync_ipo_trade
from portfolio.models import (
    InvestmentTransaction,
    TradeStatusChoices,
    TradeTypeChoices,
    TransactionSourceChoices,
)


ZERO = Decimal("0")


class Command(BaseCommand):
    help = "审计并补齐未中签港股打新的前期费用流水；默认仅 dry-run。"

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="确认写入；省略时仅审计。")

    def handle(self, *args, **options):
        trades = list(
            HkIpoSubscriptionTrade.objects.filter(allotted_lots=0)
            .select_related("listing", "member__family", "account")
            .order_by("pk")
        )
        positive = [item for item in trades if item.unallotted_fees > ZERO]
        missing_date = [
            item
            for item in positive
            if not item.listing.allotment_result_date
            and not item.listing.subscription_end_date
        ]
        if missing_date:
            details = ", ".join(
                f"{item.pk}({item.listing.stock_code})" for item in missing_date[:20]
            )
            raise CommandError(
                f"{MISSING_IPO_ACCOUNTING_DATE_MESSAGE} 相关申购记录：{details}"
            )
        missing_account = [item for item in positive if not item.account_id]
        if missing_account:
            ids = ", ".join(str(item.pk) for item in missing_account[:20])
            raise CommandError(f"以下未中签记录没有申购账户，无法同步：{ids}")

        existing = {
            item.external_id: item
            for item in InvestmentTransaction.objects.filter(
                source=TransactionSourceChoices.IPO,
                external_id__in=[f"ipo:{trade.pk}:unallotted-fee" for trade in positive],
            )
        }
        create_count = 0
        update_count = 0
        unchanged_count = 0
        for trade in positive:
            item = existing.get(f"ipo:{trade.pk}:unallotted-fee")
            if not item:
                create_count += 1
            elif self._is_current(item, trade):
                unchanged_count += 1
            else:
                update_count += 1
        total = sum((trade.unallotted_fees for trade in positive), ZERO)
        mode = "APPLY" if options["apply"] else "DRY-RUN"
        self.stdout.write(
            f"模式={mode} 未中签={len(trades)} 有费用={len(positive)} "
            f"待新增={create_count} 待校正={update_count} 已正确={unchanged_count} "
            f"费用合计={total}"
        )
        if not options["apply"]:
            self.stdout.write(self.style.WARNING("dry-run 完成；使用 --apply 才会写入数据库。"))
            return

        with transaction.atomic():
            for trade in positive:
                sync_ipo_trade(trade.pk)
            invalid = []
            for trade in positive:
                item = InvestmentTransaction.objects.filter(
                    source=TransactionSourceChoices.IPO,
                    external_id=f"ipo:{trade.pk}:unallotted-fee",
                ).first()
                if not item or item.amount != trade.unallotted_fees:
                    invalid.append(trade.pk)
            if invalid:
                raise CommandError(f"回填校验失败，IPO 记录：{', '.join(map(str, invalid[:20]))}")

        self.stdout.write(self.style.SUCCESS(f"已同步 {len(positive)} 条未中签费用流水。"))

    @staticmethod
    def _is_current(item, trade):
        expected_date = ipo_accounting_date(trade.listing)
        expected_currency = _security_identity(trade.listing.stock_code)[2]
        return (
            item.ipo_subscription_trade_id == trade.pk
            and item.account_id is not None
            and item.trade_date == expected_date
            and item.trade_type == TradeTypeChoices.OTHER_FEE_ADJUSTMENT
            and item.status == TradeStatusChoices.COMPLETED
            and item.quantity == ZERO
            and item.price == ZERO
            and item.amount == trade.unallotted_fees
            and item.fee == ZERO
            and item.tax == ZERO
            and item.currency == expected_currency
            and item.remark == "未中签前期费用"
            and (item.extra_data or {}).get("unallotted_fee_adjustment") is True
        )
