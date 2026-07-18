from decimal import Decimal

from django.db import transaction
from django.db.models import Q, Sum

from family_core.models import AssetCategory
from ipo.date_rules import ipo_accounting_date
from ipo.models import HkIpoSubscriptionTrade

from .account_sync import sync_investment_account
from .models import (
    InvestmentAccount,
    InvestmentOption,
    InvestmentTransaction,
    Security,
    TradeStatusChoices,
    TradeTypeChoices,
    TransactionSourceChoices,
    WatchlistItem,
)
from .services import rebuild_position


ZERO = Decimal("0")


def _security_identity(stock_code):
    code = (stock_code or "").strip().upper()
    cleaned = code.removeprefix("HK.").removesuffix(".HK")
    if cleaned.isdigit():
        return cleaned.zfill(5), "HK", "HKD"
    cleaned = code.removeprefix("US.").removesuffix(".US")
    return cleaned, "US", "USD"


def _stock_category(family):
    return (
        AssetCategory.objects.filter(
            Q(family=family) | Q(family__isnull=True),
            code="equity",
            is_active=True,
        )
        .order_by("-family_id", "display_order", "pk")
        .first()
    )


def _portfolio_account(ipo_trade):
    source = ipo_trade.account
    if not source:
        return None
    return sync_investment_account(source)


def _security(ipo_trade):
    listing = ipo_trade.listing
    symbol, market, currency = _security_identity(listing.stock_code)
    category = _stock_category(ipo_trade.member.family)
    security, _ = Security.objects.update_or_create(
        symbol=symbol,
        market=market,
        defaults={
            "name": listing.stock_name or listing.company_name,
            "exchange": market,
            "asset_type": "stock",
            "asset_category": category,
            "currency": currency,
            "lot_size": listing.lot_size or 0,
            "data_source": "ipo",
            "is_active": True,
        },
    )
    WatchlistItem.objects.update_or_create(
        family=ipo_trade.member.family,
        security=security,
        defaults={"member": ipo_trade.member, "is_active": True},
    )
    return security


def _upsert_transaction(external_id, defaults):
    item = InvestmentTransaction.objects.filter(
        source=TransactionSourceChoices.IPO,
        external_id=external_id,
    ).first()
    if item:
        for field, value in defaults.items():
            setattr(item, field, value)
        item.save()
        return item
    return InvestmentTransaction.objects.create(
        source=TransactionSourceChoices.IPO,
        external_id=external_id,
        **defaults,
    )


def refresh_ipo_sale_summary(ipo_trade):
    sales = InvestmentTransaction.objects.filter(
        source=TransactionSourceChoices.IPO,
        trade_type=TradeTypeChoices.SELL,
        ipo_subscription_trade=ipo_trade,
    )
    sold_total = sales.aggregate(total=Sum("quantity"))["total"] or ZERO
    realized_profit = sales.aggregate(total=Sum("realized_pnl"))["total"] or ZERO
    trading_fee = sales.aggregate(total=Sum("fee"))["total"] or ZERO
    lot_size = ipo_trade.listing.lot_size or 0
    sold_lots = int(sold_total / Decimal(lot_size)) if lot_size else 0
    latest_sale = sales.order_by("-trade_date", "-pk").first()
    if ipo_trade.allotted_lots == 0:
        status = HkIpoSubscriptionTrade.STATUS_UNALLOTTED
        realized_profit = -ipo_trade.unallotted_fees
    elif ipo_trade.allotted_lots and sold_lots >= ipo_trade.allotted_lots:
        status = HkIpoSubscriptionTrade.STATUS_CLOSED
    elif ipo_trade.allotted_lots is None:
        status = HkIpoSubscriptionTrade.STATUS_APPLYING
    else:
        status = HkIpoSubscriptionTrade.STATUS_HOLDING
    HkIpoSubscriptionTrade.objects.filter(pk=ipo_trade.pk).update(
        sold_lots=sold_lots,
        realized_profit=realized_profit,
        sell_date=(
            latest_sale.trade_date
            if latest_sale
            else (
                ipo_accounting_date(ipo_trade.listing)
                if ipo_trade.allotted_lots == 0
                else None
            )
        ),
        sell_price=latest_sale.price if latest_sale else ZERO,
        trading_fee=trading_fee,
        trade_status=status,
    )


@transaction.atomic
def sync_ipo_trade(ipo_trade_id):
    ipo_trade = (
        HkIpoSubscriptionTrade.objects.select_related(
            "listing",
            "member__family",
            "account__account_type_ref",
        )
        .filter(pk=ipo_trade_id)
        .first()
    )
    if not ipo_trade:
        return

    prefix = f"ipo:{ipo_trade.pk}:"
    old_pairs = set(
        InvestmentTransaction.objects.filter(
            source=TransactionSourceChoices.IPO,
            external_id__startswith=prefix,
            security__isnull=False,
        ).values_list("account_id", "security_id")
    )
    security = _security(ipo_trade)
    account = _portfolio_account(ipo_trade)
    if not account:
        InvestmentTransaction.objects.filter(
            source=TransactionSourceChoices.IPO,
            external_id__startswith=prefix,
        ).delete()
        for account_id, security_id in old_pairs:
            rebuild_position(
                InvestmentAccount.objects.get(pk=account_id),
                Security.objects.get(pk=security_id),
            )
        return

    listing = ipo_trade.listing
    lot_size = listing.lot_size or 0
    final_price = listing.final_price or ZERO
    allotted_lots = ipo_trade.allotted_lots or 0
    option_filter = {
        "category": InvestmentOption.CATEGORY_TRANSACTION_TYPE,
        "is_active": True,
    }
    desired_ids = []

    if allotted_lots > 0 and lot_size > 0 and final_price > 0:
        external_id = f"{prefix}buy"
        desired_ids.append(external_id)
        trade_date = ipo_accounting_date(listing)
        quantity = Decimal(allotted_lots * lot_size)
        _upsert_transaction(
            external_id,
            {
                "account": account,
                "ipo_subscription_trade": ipo_trade,
                "security": security,
                "asset_category": security.asset_category,
                "trade_date": trade_date,
                "trade_type": TradeTypeChoices.IPO,
                "trade_type_option": InvestmentOption.objects.filter(
                    code=TradeTypeChoices.IPO,
                    **option_filter,
                ).first(),
                "status": TradeStatusChoices.COMPLETED,
                "quantity": quantity,
                "price": final_price,
                "amount": quantity * final_price,
                "fee": (
                    (ipo_trade.subscription_fee or ZERO)
                    + (ipo_trade.allotment_fee or ZERO)
                    + (ipo_trade.financing_interest or ZERO)
                ),
                "tax": ZERO,
                "currency": security.currency,
                "remark": ipo_trade.remark,
                "extra_data": {},
            },
        )
    elif allotted_lots == 0 and ipo_trade.unallotted_fees > 0:
        external_id = f"{prefix}unallotted-fee"
        desired_ids.append(external_id)
        _upsert_transaction(
            external_id,
            {
                "account": account,
                "ipo_subscription_trade": ipo_trade,
                "security": security,
                "asset_category": security.asset_category,
                "trade_date": ipo_accounting_date(listing),
                "trade_type": TradeTypeChoices.OTHER_FEE_ADJUSTMENT,
                "trade_type_option": InvestmentOption.objects.filter(
                    code=TradeTypeChoices.OTHER_FEE_ADJUSTMENT,
                    **option_filter,
                ).first(),
                "status": TradeStatusChoices.COMPLETED,
                "quantity": ZERO,
                "price": ZERO,
                "amount": ipo_trade.unallotted_fees,
                "fee": ZERO,
                "tax": ZERO,
                "currency": security.currency,
                "remark": "未中签前期费用",
                "extra_data": {"unallotted_fee_adjustment": True},
            },
        )

    stale = InvestmentTransaction.objects.filter(
        source=TransactionSourceChoices.IPO,
        ipo_subscription_trade=ipo_trade,
        external_id__in={f"{prefix}buy", f"{prefix}unallotted-fee"},
    )
    if desired_ids:
        stale = stale.exclude(external_id__in=desired_ids)
    stale.delete()

    pairs = old_pairs | {(account.pk, security.pk)}
    for account_id, security_id in pairs:
        rebuild_position(
            InvestmentAccount.objects.get(pk=account_id),
            Security.objects.get(pk=security_id),
        )
    refresh_ipo_sale_summary(ipo_trade)


@transaction.atomic
def delete_synced_ipo_transactions(ipo_trade_id):
    pairs = set(
        InvestmentTransaction.objects.filter(
            source=TransactionSourceChoices.IPO,
            ipo_subscription_trade_id=ipo_trade_id,
            security__isnull=False,
        ).values_list("account_id", "security_id")
    )
    InvestmentTransaction.objects.filter(
        source=TransactionSourceChoices.IPO,
        ipo_subscription_trade_id=ipo_trade_id,
    ).delete()
    for account_id, security_id in pairs:
        rebuild_position(
            InvestmentAccount.objects.get(pk=account_id),
            Security.objects.get(pk=security_id),
        )
