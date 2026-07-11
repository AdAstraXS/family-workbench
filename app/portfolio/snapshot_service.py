from decimal import Decimal

from django.db import transaction

from .models import PortfolioSnapshot, PortfolioSnapshotPositionLine
from .valuation import value_portfolio


ZERO = Decimal("0")


@transaction.atomic
def create_portfolio_snapshot(family, accounts, snapshot_date, currency, member=None):
    accounts = list(accounts)
    valuation = value_portfolio(
        accounts,
        currency,
        snapshot_date,
        refresh_positions=True,
    )
    snapshot, _ = PortfolioSnapshot.objects.update_or_create(
        family=family,
        member=member,
        account=None,
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
            "extra_data": {"missing_exchange_rates": valuation["missing_rates"]},
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
            price=position.valuation_price,
            currency=position.security.currency,
            fx_rate=position.valuation_fx_rate,
            market_value_original=position.valuation_market_value_original,
            market_value=position.valuation_market_value,
            cost_original=position.valuation_cost_original,
            cost=position.valuation_cost,
            unrealized_pnl=(
                position.valuation_market_value - position.valuation_cost
            ),
        )
        for position in valuation["positions"]
        if position.valuation_fx_rate is not None
    )
    PortfolioSnapshotPositionLine.objects.bulk_create(lines)
    return snapshot
