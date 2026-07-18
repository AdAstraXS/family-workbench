from decimal import Decimal

from django.db import transaction

from ledger.models import AssetBalanceSnapshot

from .historical_valuation import slice_valuation, value_historical_portfolio
from .models import PortfolioSnapshot, PortfolioSnapshotPositionLine


ZERO = Decimal("0")


@transaction.atomic
def create_portfolio_snapshot(
    family,
    accounts,
    snapshot_date,
    currency,
    member=None,
    account=None,
    *,
    valuation=None,
    ledger_snapshot=None,
):
    accounts = list(accounts)
    valuation = valuation or value_historical_portfolio(
        accounts,
        currency,
        snapshot_date,
        ledger_snapshot=ledger_snapshot,
    )
    snapshot, _created = PortfolioSnapshot.objects.update_or_create(
        family=family,
        member=member,
        account=account,
        snapshot_date=snapshot_date,
        currency=currency,
        defaults={
            "total_cash": valuation["total_cash"],
            "total_market_value": valuation["total_market_value"],
            "total_asset": valuation["total_asset"],
            "total_cost": valuation["total_cost"],
            "total_pnl": valuation["total_pnl"],
            "pnl_ratio": (
                valuation["total_pnl"] / valuation["total_cost"]
                if valuation["total_cost"]
                else ZERO
            ),
            "extra_data": {
                "complete": valuation["complete"],
                "missing_exchange_rates": valuation["missing_rates"],
                "stale_prices": valuation["stale_prices"],
                "missing_prices": valuation["missing_prices"],
                "valuation_errors": valuation["errors"],
            },
        },
    )
    snapshot.position_lines.all().delete()
    account_map = {item.pk: item for item in accounts}
    lines = [
        PortfolioSnapshotPositionLine(
            snapshot=snapshot,
            account=account_map[row["account_id"]],
            asset_type="cash",
            asset_name=f"{row['currency']} 现金",
            quantity=row["amount"] or ZERO,
            price=Decimal("1"),
            currency=row["currency"],
            fx_rate=row["fx_rate"],
            market_value_original=row["amount"] or ZERO,
            market_value=row["converted"],
            cost_original=row["amount"] or ZERO,
            cost=row["converted"],
            unrealized_pnl=ZERO,
        )
        for row in valuation["cash_lines"]
    ]
    lines.extend(
        PortfolioSnapshotPositionLine(
            snapshot=snapshot,
            account=position.account,
            security=position.security,
            asset_type=position.security.asset_type,
            asset_name=position.security.name,
            quantity=position.quantity,
            price=position.price,
            currency=position.security.currency,
            fx_rate=position.fx_rate,
            market_value_original=position.market_value_original,
            market_value=position.market_value,
            cost_original=position.cost_original,
            cost=position.cost,
            unrealized_pnl=position.market_value - position.cost,
        )
        for position in valuation["positions"]
        if position.price is not None and position.fx_rate is not None
    )
    PortfolioSnapshotPositionLine.objects.bulk_create(lines)
    return snapshot


@transaction.atomic
def create_portfolio_snapshots_for_date(
    family,
    accounts,
    snapshot_date,
    currency,
    *,
    require_complete=False,
):
    accounts = list(accounts)
    ledger_snapshot = (
        AssetBalanceSnapshot.objects.filter(
            family=family,
            snapshot_date=snapshot_date,
            is_draft=False,
        )
        .order_by("-created_at", "-pk")
        .first()
    )
    valuation = value_historical_portfolio(
        accounts,
        currency,
        snapshot_date,
        ledger_snapshot=ledger_snapshot,
    )
    if require_complete and not valuation["complete"]:
        missing = len(valuation["missing_prices"])
        rates = len(valuation["missing_rates"])
        errors = len(valuation["errors"])
        raise ValueError(
            f"{snapshot_date} 估值不完整：缺价 {missing}、缺汇率 {rates}、流水错误 {errors}。"
        )

    snapshots = []
    snapshots.append(
        create_portfolio_snapshot(
            family,
            accounts,
            snapshot_date,
            currency,
            valuation=valuation,
            ledger_snapshot=ledger_snapshot,
        )
    )
    member_ids = sorted({item.member_id for item in accounts})
    for member_id in member_ids:
        member_accounts = [item for item in accounts if item.member_id == member_id]
        member_valuation = slice_valuation(
            valuation, [item.pk for item in member_accounts]
        )
        snapshots.append(
            create_portfolio_snapshot(
                family,
                member_accounts,
                snapshot_date,
                currency,
                member=member_accounts[0].member,
                valuation=member_valuation,
                ledger_snapshot=ledger_snapshot,
            )
        )
    for item in accounts:
        account_valuation = slice_valuation(valuation, [item.pk])
        snapshots.append(
            create_portfolio_snapshot(
                family,
                [item],
                snapshot_date,
                currency,
                member=item.member,
                account=item,
                valuation=account_valuation,
                ledger_snapshot=ledger_snapshot,
            )
        )
    return snapshots
