from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.core.exceptions import ValidationError
from django.db import transaction

from ipo.models import HkIpoSubscriptionTrade
from portfolio.ipo_sync import _portfolio_account, _security
from portfolio.models import (
    InvestmentOption,
    InvestmentTransaction,
    InvestmentPosition,
    TradeStatusChoices,
    TradeTypeChoices,
    TransactionSourceChoices,
)
from portfolio.services import rebuild_position


ZERO = Decimal("0")
TOLERANCE = Decimal("0.02")


class Command(BaseCommand):
    help = "审计并回填历史港股打新买入及汇总卖出流水；默认仅 dry-run。"

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="确认写入；省略时只审计。")
        parser.add_argument("--limit", type=int, help="仅处理前 N 条，供隔离演练使用。")
        parser.add_argument(
            "--accept-implied-gross",
            action="store_true",
            help="对无法由卖出价重建的历史记录，显式采用汇总净损益反推成交金额。",
        )

    def handle(self, *args, **options):
        apply_changes = options["apply"]
        trades = list(
            HkIpoSubscriptionTrade.objects.filter(allotted_lots__gt=0)
            .select_related("listing", "member__family", "account")
            .order_by("pk")
        )
        if options.get("limit"):
            trades = trades[: options["limit"]]

        plans = []
        errors = []
        warnings = []
        skipped = 0
        for ipo_trade in trades:
            existing_types = set(
                InvestmentTransaction.objects.filter(
                    source=TransactionSourceChoices.IPO,
                    ipo_subscription_trade=ipo_trade,
                ).values_list("trade_type", flat=True)
            )
            need_buy = TradeTypeChoices.IPO not in existing_types
            need_sell = (ipo_trade.sold_lots or 0) > 0 and TradeTypeChoices.SELL not in existing_types
            if not need_buy and not need_sell:
                skipped += 1
                continue
            listing = ipo_trade.listing
            lot_size = listing.lot_size or 0
            final_price = listing.final_price or ZERO
            buy_date = listing.allotment_result_date or (
                listing.subscription_end_date + timedelta(days=2)
                if listing.subscription_end_date
                else ipo_trade.application_date
            )
            required = []
            if not ipo_trade.account_id:
                required.append("申购账户")
            if not lot_size:
                required.append("每手股数")
            if not final_price:
                required.append("最终定价")
            if not buy_date:
                required.append("中签日期")
            if need_sell and not ipo_trade.sell_date:
                required.append("卖出日期")
            if need_sell and not ipo_trade.sell_price:
                required.append("卖出价格")
            if required:
                errors.append(f"IPO #{ipo_trade.pk} {listing}: 缺少{'、'.join(required)}")
                continue
            if need_sell and ipo_trade.sell_date < buy_date:
                buy_date = ipo_trade.sell_date

            gross_profit = self._historical_gross_profit(ipo_trade)
            expected = (
                gross_profit
                - ipo_trade.upfront_fees_for_lots(ipo_trade.sold_lots)
                - (ipo_trade.trading_fee or ZERO)
            )
            if need_sell and abs(expected - (ipo_trade.realized_profit or ZERO)) > TOLERANCE:
                message = (
                    f"IPO #{ipo_trade.pk} {listing}: 汇总净损益 {ipo_trade.realized_profit} "
                    f"与可重建值 {expected} 相差 {expected - (ipo_trade.realized_profit or ZERO)}"
                )
                if not options["accept_implied_gross"]:
                    errors.append(message)
                    continue
                warnings.append(message + "；将由汇总净损益反推历史成交金额")
                gross_profit = (
                    (ipo_trade.realized_profit or ZERO)
                    + ipo_trade.upfront_fees_for_lots(ipo_trade.sold_lots)
                    + (ipo_trade.trading_fee or ZERO)
                )
            plans.append((ipo_trade, need_buy, need_sell, buy_date, gross_profit))

        self.stdout.write(
            f"模式={'APPLY' if apply_changes else 'DRY-RUN'} 总记录={len(trades)} "
            f"待回填={len(plans)} 已完整跳过={skipped} 警告={len(warnings)} 错误={len(errors)}"
        )
        for warning in warnings[:50]:
            self.stdout.write(self.style.WARNING(warning))
        for error in errors[:50]:
            self.stdout.write(self.style.ERROR(error))
        if len(errors) > 50:
            self.stdout.write(self.style.ERROR(f"另有 {len(errors) - 50} 条错误未展开。"))
        if errors:
            raise CommandError("存在无法安全回填的记录；数据库未修改。")
        if not apply_changes:
            self.stdout.write(self.style.WARNING("dry-run 完成；使用 --apply 才会写入数据库。"))
            return

        with transaction.atomic():
            for ipo_trade, need_buy, need_sell, buy_date, gross_profit in plans:
                self._apply_trade(ipo_trade, need_buy, need_sell, buy_date, gross_profit)
        self.stdout.write(self.style.SUCCESS(f"已安全回填 {len(plans)} 条历史申购记录。"))

    @staticmethod
    def _historical_gross_profit(ipo_trade):
        calculated = (
            ((ipo_trade.sell_price or ZERO) - (ipo_trade.listing.final_price or ZERO))
            * Decimal(ipo_trade.sold_lots or 0)
            * Decimal(ipo_trade.listing.lot_size or 0)
        )
        source_gross = (ipo_trade.extra_data or {}).get("source_gross_profit")
        if source_gross in (None, ""):
            return calculated
        source_gross = Decimal(str(source_gross))
        fees = ipo_trade.upfront_fees_for_lots(ipo_trade.sold_lots) + (
            ipo_trade.trading_fee or ZERO
        )
        target = ipo_trade.realized_profit or ZERO
        return min((calculated, source_gross), key=lambda value: abs(value - fees - target))

    def _apply_trade(self, ipo_trade, need_buy, need_sell, buy_date, gross_profit):
        listing = ipo_trade.listing
        account = _portfolio_account(ipo_trade)
        security = _security(ipo_trade)
        lot_size = listing.lot_size
        option_filter = {
            "category": InvestmentOption.CATEGORY_TRANSACTION_TYPE,
            "is_active": True,
        }
        common = {
            "account": account,
            "ipo_subscription_trade": ipo_trade,
            "security": security,
            "asset_category": security.asset_category,
            "status": TradeStatusChoices.COMPLETED,
            "currency": security.currency,
            "remark": ipo_trade.remark,
        }
        if need_buy:
            quantity = Decimal(ipo_trade.allotted_lots * lot_size)
            InvestmentTransaction.objects.create(
                source=TransactionSourceChoices.IPO,
                external_id=f"ipo:{ipo_trade.pk}:buy",
                trade_date=buy_date,
                trade_type=TradeTypeChoices.IPO,
                trade_type_option=InvestmentOption.objects.filter(
                    code=TradeTypeChoices.IPO, **option_filter
                ).first(),
                quantity=quantity,
                price=listing.final_price,
                amount=quantity * listing.final_price,
                fee=ipo_trade.upfront_fees,
                tax=ZERO,
                extra_data={"historical_backfill": True},
                **common,
            )
        if need_sell:
            quantity = Decimal(ipo_trade.sold_lots * lot_size)
            sold_cost = quantity * listing.final_price
            InvestmentTransaction.objects.create(
                source=TransactionSourceChoices.IPO,
                external_id=f"ipo:{ipo_trade.pk}:sell:legacy",
                trade_date=ipo_trade.sell_date,
                trade_type=TradeTypeChoices.SELL,
                trade_type_option=InvestmentOption.objects.filter(
                    code=TradeTypeChoices.SELL, **option_filter
                ).first(),
                quantity=quantity,
                price=ipo_trade.sell_price,
                amount=sold_cost + gross_profit,
                fee=ipo_trade.trading_fee or ZERO,
                tax=ZERO,
                extra_data={
                    "historical_backfill": True,
                    "summary_sale": True,
                    "amount_uses_historical_gross_profit": True,
                },
                **common,
            )
        try:
            rebuild_position(account, security)
        except ValidationError as exc:
            raise CommandError(f"IPO #{ipo_trade.pk} {listing} 回填后持仓重建失败：{exc}") from exc
        expected_quantity = Decimal(
            max((ipo_trade.allotted_lots or 0) - (ipo_trade.sold_lots or 0), 0)
            * lot_size
        )
        actual_quantity = InvestmentPosition.objects.get(
            account=account,
            security=security,
        ).quantity
        if actual_quantity != expected_quantity:
            raise CommandError(
                f"IPO #{ipo_trade.pk} 回填后持仓不一致：持仓 {actual_quantity} / 预期 {expected_quantity}"
            )
        realized = sum(
            InvestmentTransaction.objects.filter(
                source=TransactionSourceChoices.IPO,
                ipo_subscription_trade=ipo_trade,
                trade_type=TradeTypeChoices.SELL,
            ).values_list("realized_pnl", flat=True),
            ZERO,
        )
        if abs(realized - (ipo_trade.realized_profit or ZERO)) > TOLERANCE:
            raise CommandError(
                f"IPO #{ipo_trade.pk} 回填后损益不一致：流水 {realized} / 汇总 {ipo_trade.realized_profit}"
            )
