"""Fail-closed capacity snapshots derived from the local portfolio ledger."""

from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
import json

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from portfolio.historical_valuation import value_historical_portfolio
from portfolio.models import (
    InvestmentAccount,
    InvestmentCashMovement,
    InvestmentPosition,
    InvestmentTransaction,
    OptionContract,
    Security,
    SecurityPriceRecord,
)

from .models import DataStatus, WheelBrokerAccountSnapshot


MONEY_STEP = Decimal("0.0001")
PARTICIPATING_ACCOUNT_NAMES = ("致富证券（公户）", "盈透证券")


class CapacityImportError(ValueError):
    pass


@dataclass(frozen=True)
class CapacityEvidence:
    account_id: int
    source_reference: str
    source_as_of: object
    currency: str
    settled_cash: Decimal
    unsettled_cash: Decimal
    nav: Decimal
    reserved_cash: Decimal
    margin_loan_balance: Decimal
    uses_margin: bool
    positions_summary: dict
    open_obligations: dict


@dataclass(frozen=True)
class ImportResult:
    evidence: CapacityEvidence
    snapshot_created: bool
    snapshot_id: int | None


def capacity_snapshot_stale_reasons(snapshot):
    """Return portfolio evidence changes newer than a formal capacity snapshot."""
    if snapshot is None:
        return ["尚无正式容量快照"]
    cutoff = snapshot.source_as_of
    reasons = []
    transaction_changed = InvestmentTransaction.objects.filter(
        account_id=snapshot.account_id,
        updated_at__gt=cutoff,
    ).exists()
    cash_changed = InvestmentCashMovement.objects.filter(
        account_id=snapshot.account_id,
        updated_at__gt=cutoff,
    ).exists()
    position_changed = InvestmentPosition.objects.filter(
        account_id=snapshot.account_id,
        updated_at__gt=cutoff,
    ).exists()
    if transaction_changed or cash_changed or position_changed:
        reasons.append("投资组合流水、现金或持仓在确认后发生变化")

    position_items = snapshot.positions_summary.get("items", []) if isinstance(snapshot.positions_summary, dict) else []
    security_ids = {
        item.get("security_id")
        for item in position_items
        if isinstance(item, dict) and isinstance(item.get("security_id"), int)
    }
    if security_ids:
        latest_price = SecurityPriceRecord.objects.filter(
            security_id__in=security_ids,
        ).aggregate(value=Max("fetched_at"))["value"]
        if latest_price and latest_price > cutoff:
            reasons.append("持仓行情在确认后已更新，账户净值需要重新确认")
    return reasons


def _money(value):
    return Decimal(value).quantize(MONEY_STEP)


def _portfolio_error(valuation):
    return (
        "投资组合当日估值不完整："
        f"缺价 {len(valuation['missing_prices'])}、"
        f"过期价格 {len(valuation['stale_prices'])}、"
        f"缺汇率 {len(valuation['missing_rates'])}、"
        f"流水错误 {len(valuation['errors'])}。"
    )


def _position_item(position):
    security = position.security
    item = {
        "security_id": security.pk,
        "symbol": security.symbol,
        "asset_type": security.asset_type,
        "quantity": str(_money(position.quantity)),
        "currency": security.currency,
        "market_value_usd": str(_money(position.market_value)),
    }
    if (
        security.asset_type == Security.TYPE_STOCK
        and position.quantity
        and getattr(position, "cost_original", None) is not None
    ):
        item["average_cost"] = str(
            _money(position.cost_original / position.quantity)
        )
    option = getattr(security, "option_contract", None)
    if option:
        item.update(
            {
                "underlying": option.underlying.symbol,
                "option_type": option.option_type,
                "strike": str(_money(option.strike_price)),
                "expiration": option.expiration_date.isoformat(),
                "multiplier": option.multiplier,
            }
        )
    return item


def build_portfolio_capacity(
    *,
    account_id,
    confirm_no_margin=False,
    confirm_no_open_orders=False,
    source_as_of=None,
):
    if not confirm_no_margin:
        raise CapacityImportError("必须明确确认该账户当前没有融资或借贷。")
    if not confirm_no_open_orders:
        raise CapacityImportError("必须明确确认券商端没有尚未录入投资组合的未成交订单。")
    try:
        account = InvestmentAccount.objects.select_related(
            "bank_account", "bank_account__family"
        ).get(pk=account_id, bank_account__is_active=True)
    except InvestmentAccount.DoesNotExist:
        raise CapacityImportError("找不到有效的投资账户。") from None
    if account.account_name not in PARTICIPATING_ACCOUNT_NAMES:
        raise CapacityImportError("该账户不在已确认的车轮参与账户范围内。")

    source_as_of = source_as_of or timezone.now()
    if timezone.is_naive(source_as_of):
        raise CapacityImportError("source_as_of 必须带时区。")
    on_date = timezone.localtime(source_as_of).date()
    valuation = value_historical_portfolio([account], "USD", on_date)
    if not valuation["complete"]:
        raise CapacityImportError(_portfolio_error(valuation))

    usd_cash = sum(
        (
            row["amount"]
            for row in valuation["cash_lines"]
            if row["account_id"] == account.pk and row["currency"].upper() == "USD"
        ),
        Decimal("0"),
    )
    pending = InvestmentCashMovement.objects.filter(
        account=account,
        currency="USD",
        movement_date__lte=on_date,
        settlement_date__gt=on_date,
    )
    unsettled_inflows = sum(
        (item.amount for item in pending if item.amount > 0), Decimal("0")
    )
    pending_outflows = sum(
        (-item.amount for item in pending if item.amount < 0), Decimal("0")
    )
    settled_cash = _money(usd_cash - unsettled_inflows)
    unsettled_cash = _money(unsettled_inflows)
    nav = _money(valuation["total_asset"])
    if settled_cash < 0 or nav <= 0:
        raise CapacityImportError("本地投资组合显示已结算 USD 现金为负或 NAV 非正。")

    positions = valuation["positions"]
    position_items = [_position_item(position) for position in positions]
    stock_quantities = {
        position.security.pk: position.quantity
        for position in positions
        if position.security.asset_type == "stock" and position.quantity > 0
    }
    obligations = []
    reserved_cash = Decimal("0")
    covered_shares_used = {}
    for position in positions:
        if position.quantity >= 0:
            continue
        option = getattr(position.security, "option_contract", None)
        if option is None:
            raise CapacityImportError("投资组合含有无法按车轮规则解释的非期权空头持仓。")
        contracts = abs(position.quantity)
        if contracts != contracts.to_integral_value():
            raise CapacityImportError("期权持仓张数必须是整数。")
        obligation = {
            "kind": (
                "cash_secured_put"
                if option.option_type == OptionContract.PUT
                else "covered_call"
            ),
            "symbol": option.underlying.symbol,
            "contracts": str(_money(contracts)),
            "strike": str(_money(option.strike_price)),
            "expiration": option.expiration_date.isoformat(),
            "multiplier": option.multiplier,
        }
        if option.option_type == OptionContract.PUT:
            required_cash = option.strike_price * option.multiplier * contracts
            reserved_cash += required_cash
            obligation["cash_requirement"] = str(_money(required_cash))
        else:
            required_shares = contracts * option.multiplier
            available_shares = stock_quantities.get(option.underlying_id, Decimal("0"))
            covered_shares_used[option.underlying_id] = (
                covered_shares_used.get(option.underlying_id, Decimal("0"))
                + required_shares
            )
            if covered_shares_used[option.underlying_id] > available_shares:
                raise CapacityImportError(
                    f"{option.underlying.symbol} 的空头 Call 没有足够正股覆盖。"
                )
            obligation["required_shares"] = str(_money(required_shares))
            obligation["cash_requirement"] = "0.0000"
        obligations.append(obligation)
    if pending_outflows:
        obligations.append(
            {
                "kind": "pending_cash_outflow",
                "symbol": "USD",
                "cash_requirement": "0.0000",
                "amount_already_deducted": str(_money(pending_outflows)),
            }
        )
    reserved_cash = _money(reserved_cash)
    if reserved_cash > settled_cash:
        raise CapacityImportError("现有 Sell Put 的全额现金占用超过已结算 USD 现金。")

    positions_summary = {
        "source": "portfolio_current_valuation",
        "valuation_date": on_date.isoformat(),
        "count": len(position_items),
        "items": position_items,
        "complete": True,
    }
    open_obligations = {
        "source": "portfolio_positions",
        "no_unrecorded_open_orders_confirmed": True,
        "count": len(obligations),
        "items": obligations,
        "reserved_cash": str(reserved_cash),
        "complete": True,
    }
    fingerprint_input = {
        "account_id": account.pk,
        "valuation_date": on_date.isoformat(),
        "settled_cash": str(settled_cash),
        "unsettled_cash": str(unsettled_cash),
        "nav": str(nav),
        "reserved_cash": str(reserved_cash),
        "positions": position_items,
        "obligations": obligations,
    }
    digest = sha256(
        json.dumps(
            fingerprint_input,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return CapacityEvidence(
        account_id=account.pk,
        source_reference=f"portfolio:{account.pk}:{on_date.isoformat()}:{digest}",
        source_as_of=source_as_of,
        currency="USD",
        settled_cash=settled_cash,
        unsettled_cash=unsettled_cash,
        nav=nav,
        reserved_cash=reserved_cash,
        margin_loan_balance=Decimal("0"),
        uses_margin=False,
        positions_summary=positions_summary,
        open_obligations=open_obligations,
    )


@transaction.atomic
def import_portfolio_capacity(*, evidence, commit=False):
    account = InvestmentAccount.objects.select_related("bank_account").get(
        pk=evidence.account_id
    )
    existing = WheelBrokerAccountSnapshot.objects.filter(
        family=account.family,
        account=account,
        source_kind=WheelBrokerAccountSnapshot.SOURCE_PORTFOLIO_READONLY,
        source_reference=evidence.source_reference,
    ).first()
    if existing:
        return ImportResult(evidence, False, existing.pk)
    if not commit:
        return ImportResult(evidence, False, None)
    snapshot = WheelBrokerAccountSnapshot.objects.create(
        family=account.family,
        account=account,
        source_kind=WheelBrokerAccountSnapshot.SOURCE_PORTFOLIO_READONLY,
        source_reference=evidence.source_reference,
        currency=evidence.currency,
        settled_cash=evidence.settled_cash,
        unsettled_cash=evidence.unsettled_cash,
        nav=evidence.nav,
        reserved_cash=evidence.reserved_cash,
        margin_loan_balance=evidence.margin_loan_balance,
        uses_margin=evidence.uses_margin,
        positions_summary=evidence.positions_summary,
        open_obligations=evidence.open_obligations,
        source_as_of=evidence.source_as_of,
        data_status=DataStatus.COMPLETE,
    )
    return ImportResult(evidence, True, snapshot.pk)
