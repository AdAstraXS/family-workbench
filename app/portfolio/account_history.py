"""Read-only account views of a single, explicitly dated portfolio snapshot."""

from collections import defaultdict
from decimal import Decimal

from django.db.models import Sum

from ledger.models import AssetBalanceSnapshot

from .historical_valuation import account_ids_as_of
from .models import InvestmentTransaction, PortfolioSnapshot, TradeStatusChoices
from .valuation import exchange_rate, exchange_rate_cache


ZERO = Decimal("0")


def total_or_none(values):
    values = list(values)
    return sum(values, ZERO) if all(value is not None for value in values) else None


def family_snapshots(family):
    return PortfolioSnapshot.objects.filter(
        family=family, member=None, account=None, currency=family.base_currency,
    ).order_by("-snapshot_date", "-pk")


class SnapshotRates:
    """Keep frozen valuations and P&L on the same documented FX basis.

    Conflicting frozen rates are not silently replaced with today's or another
    source's rates. Cross currency display uses the same snapshot base bridge.
    """

    def __init__(self, snapshot, lines):
        self.snapshot = snapshot
        candidates = defaultdict(set)
        for line in lines:
            if line.fx_rate > 0:
                candidates[line.currency.upper()].add(line.fx_rate)
        self.rates = {
            currency: next(iter(values)) if len(values) == 1 else None
            for currency, values in candidates.items()
        }
        self.rates[snapshot.currency.upper()] = Decimal("1")
        self.ledger = AssetBalanceSnapshot.objects.filter(
            family=snapshot.family, snapshot_date=snapshot.snapshot_date,
            is_draft=False, base_currency=snapshot.currency,
        ).first()
        self.missing = set()

    def to_base(self, currency):
        currency = currency.upper()
        if currency not in self.rates:
            field = {"USD": "usd_to_base", "HKD": "hkd_to_base"}.get(currency)
            rate = getattr(self.ledger, field, None) if field else None
            self.rates[currency] = (
                rate if rate and rate > 0 else exchange_rate(
                    currency, self.snapshot.currency, self.snapshot.snapshot_date,
                )
            )
        return self.rates[currency]

    def convert(self, amount, source, target):
        if amount is None:
            return None
        if source.upper() == target.upper() or amount == ZERO:
            return amount
        source_rate, target_rate = self.to_base(source), self.to_base(target)
        if source_rate is None or target_rate is None:
            self.missing.add(f"{source}/{target}")
            return None
        return amount * source_rate / target_rate


@exchange_rate_cache()
def snapshot_account_data(snapshot, accounts, selected_currency):
    """Use saved lines for holdings; transaction P&L is cumulative to cutoff."""
    lines = list(snapshot.position_lines.select_related(
        "account__bank_account__member", "account__bank_account__family", "security",
    ))
    rates = SnapshotRates(snapshot, lines)
    scopes = {
        item.account_id: item
        for item in PortfolioSnapshot.objects.filter(
            family=snapshot.family, snapshot_date=snapshot.snapshot_date,
            currency=snapshot.currency, account__isnull=False,
        )
    }
    ids = account_ids_as_of(snapshot.family, snapshot.snapshot_date)
    ids.update(line.account_id for line in lines)
    ids.update(scopes)
    accounts = [account for account in accounts if account.pk in ids]
    account_map = {account.pk: account for account in accounts}
    by_account = defaultdict(list)
    for line in lines:
        if line.account_id in account_map:
            by_account[line.account_id].append(line)

    profits = list(InvestmentTransaction.objects.filter(
        account__in=accounts, trade_date__lte=snapshot.snapshot_date,
        status__in=[TradeStatusChoices.PARTIAL, TradeStatusChoices.COMPLETED],
    ).values("account_id", "security_id", "currency").annotate(amount=Sum("realized_pnl")))
    by_account_profit = defaultdict(list)
    by_security_profit = defaultdict(list)
    for profit in profits:
        by_account_profit[profit["account_id"]].append(rates.convert(
            profit["amount"], profit["currency"], selected_currency,
        ))
        by_security_profit[(profit["account_id"], profit["security_id"])].append(profit)

    rows, holdings = [], []
    for account in accounts:
        account_lines = by_account[account.pk]
        scope = scopes.get(account.pk)
        details = (scope.extra_data if scope else snapshot.extra_data) or {}
        issues = {
            key: [item for item in details.get(key, []) if item.get("account_id") == account.pk]
            for key in ("missing_prices", "missing_exchange_rates", "valuation_errors", "stale_prices")
        }
        has_evidence = bool(scope or account_lines)
        row = {"account": account, "today_pnl": None}
        for field, cash in (("cash", True), ("market_value", False)):
            values = [rates.convert(line.market_value, snapshot.currency, selected_currency)
                      for line in account_lines if (line.asset_type == "cash") == cash]
            row[field] = total_or_none(values) if has_evidence else None
        row["unrealized"] = total_or_none(
            rates.convert(line.unrealized_pnl, snapshot.currency, selected_currency)
            for line in account_lines if line.asset_type != "cash"
        ) if has_evidence else None
        # Missing lines were omitted when the snapshot was saved. Never present
        # their partial sum as a complete account balance.
        if issues["missing_exchange_rates"] or issues["valuation_errors"]:
            row["cash"] = row["market_value"] = row["unrealized"] = None
        elif issues["missing_prices"]:
            row["market_value"] = row["unrealized"] = None
        if scope and not account_lines and (scope.total_cash or scope.total_market_value):
            row["cash"] = row["market_value"] = row["unrealized"] = None
            has_evidence = False
        row["total_asset"] = total_or_none((row["cash"], row["market_value"]))
        row["total_asset_cny"] = rates.convert(row["total_asset"], selected_currency, "CNY")
        row["position_ratio"] = (
            row["market_value"] / row["total_asset"] * 100 if row["total_asset"]
            else (ZERO if row["total_asset"] == ZERO else None)
        )
        row["cash_ratio"] = 100 - row["position_ratio"] if row["position_ratio"] is not None else None
        row["realized"] = total_or_none(by_account_profit[account.pk])
        row["history_note"] = (
            "缺少快照明细" if not has_evidence else
            "数据不完整" if any(issues[key] for key in ("missing_prices", "missing_exchange_rates", "valuation_errors")) else
            "价格需核对" if issues["stale_prices"] else ""
        )
        rows.append(row)
        for line in account_lines:
            if line.asset_type == "cash":
                continue
            entries = by_security_profit[(account.pk, line.security_id)]
            holdings.append({
                "account": account, "security": line.security, "name": line.asset_name,
                "quantity": line.quantity, "price": line.price, "price_as_of": line.price_as_of,
                "currency": line.currency, "market_value": line.market_value_original,
                "cost": line.cost_original,
                "unrealized": line.market_value_original - line.cost_original,
                "realized": total_or_none(rates.convert(item["amount"], item["currency"], line.currency) for item in entries),
                "pricing_status": line.get_pricing_status_display() or "未记录价格状态",
            })
    rows.sort(key=lambda row: (row["total_asset"] is not None, row["total_asset"] or ZERO), reverse=True)
    return {
        "accounts": accounts, "account_rows": rows, "historical_positions": holdings,
        "positions": [], "balance_snapshot_date": snapshot.snapshot_date,
        "balance_snapshot": snapshot, "historical_view": True,
        "history_missing_rates": sorted(rates.missing),
        "missing_exchange_rates": bool(rates.missing),
    }
