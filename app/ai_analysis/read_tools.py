"""Read-only, module-scoped evidence builders for the global AI feature."""

from collections import defaultdict
from decimal import Decimal

from ledger.models import AssetBalanceSnapshot
from portfolio.models import InvestmentAccount, PortfolioSnapshot


SCOPE_PERSONAL = "personal"
SCOPE_FAMILY = "family"
VALID_SCOPES = {SCOPE_PERSONAL, SCOPE_FAMILY}
ZERO = Decimal("0")


class GlobalAiReadError(ValueError):
    """Raised when the trusted host context cannot produce safe evidence."""


def _validate_context(actor, scope):
    if not actor or not actor.pk or not actor.is_active:
        raise GlobalAiReadError("当前成员无效。")
    if scope not in VALID_SCOPES:
        raise GlobalAiReadError("不支持的数据范围。")


def _decimal(value):
    return str(value if value is not None else ZERO)


def _ledger_rate_available(snapshot, currency):
    currency = (currency or "").upper()
    base_currency = snapshot.base_currency.upper()
    if currency == base_currency:
        return True
    if currency == "USD":
        return snapshot.usd_to_base > ZERO
    if currency == "HKD":
        return snapshot.hkd_to_base > ZERO
    return False


def ledger_asset_snapshot(actor, *, scope=SCOPE_PERSONAL, snapshot_id=None):
    """Return one formal ledger snapshot without consulting portfolio data."""

    _validate_context(actor, scope)
    snapshots = AssetBalanceSnapshot.objects.filter(
        family=actor.family,
        is_draft=False,
    )
    if snapshot_id is not None:
        snapshot = snapshots.filter(pk=snapshot_id).first()
    else:
        snapshot = snapshots.order_by("-snapshot_date", "-created_at", "-pk").first()
    if snapshot is None:
        raise GlobalAiReadError("没有可读取的正式账户资产快照。")

    entries = snapshot.entries.select_related(
        "member",
        "account__account_type_ref",
        "asset_category",
    )
    if scope == SCOPE_PERSONAL:
        entries = entries.filter(member=actor)
    entries = list(entries.order_by("member__display_order", "display_order", "pk"))

    missing_rates = sorted(
        {entry.currency.upper() for entry in entries if not _ledger_rate_available(snapshot, entry.currency)}
    )
    warnings = []
    if not entries:
        warnings.append("所选正式快照没有当前范围的账户明细。")
    if missing_rates:
        warnings.append("部分外币缺少可核验汇率，不能提供完整本位币总额。")
    if snapshot_id is None:
        same_date_count = snapshots.filter(snapshot_date=snapshot.snapshot_date).count()
        if same_date_count > 1:
            warnings.append("同一日期存在多份正式快照，当前使用最新创建的一份。")

    complete = bool(entries) and not missing_rates
    total = sum((entry.base_amount for entry in entries), ZERO) if complete else None
    return {
        "module": "ledger",
        "scope": scope,
        "snapshot_id": snapshot.pk,
        "snapshot_date": snapshot.snapshot_date.isoformat(),
        "base_currency": snapshot.base_currency,
        "complete": complete,
        "total_base_amount": _decimal(total) if total is not None else None,
        "missing_exchange_rates": missing_rates,
        "warnings": warnings,
        "accounts": [
            {
                "member_id": entry.member_id,
                "member_name": entry.member.display_name,
                "account_id": entry.account_id,
                "account_name": (
                    entry.account.account_name if entry.account_id else entry.account_name
                ),
                "account_type": (
                    entry.account.account_type_ref.name
                    if entry.account_id and entry.account.account_type_ref_id
                    else None
                ),
                "asset_category": entry.asset_category.name if entry.asset_category_id else None,
                "currency": entry.currency,
                "original_amount": _decimal(entry.original_amount),
                "base_amount": _decimal(entry.base_amount),
            }
            for entry in entries
        ],
    }


def portfolio_account_snapshot(
    actor,
    *,
    account_id,
    scope=SCOPE_PERSONAL,
    snapshot_id=None,
):
    """Return one account-level portfolio snapshot without consulting ledger data."""

    _validate_context(actor, scope)
    accounts = InvestmentAccount.objects.select_related(
        "bank_account__member",
        "bank_account__family",
    ).filter(bank_account__family=actor.family)
    if scope == SCOPE_PERSONAL:
        accounts = accounts.filter(bank_account__member=actor)
    account = accounts.filter(pk=account_id).first()
    if account is None:
        raise GlobalAiReadError("投资账户不可用。")

    snapshots = PortfolioSnapshot.objects.filter(
        family=actor.family,
        member_id=account.member_id,
        account=account,
    )
    if snapshot_id is not None:
        snapshot = snapshots.filter(pk=snapshot_id).first()
    else:
        snapshot = snapshots.order_by("-snapshot_date", "-pk").first()
    if snapshot is None:
        raise GlobalAiReadError("该投资账户没有可读取的账户级快照。")

    lines = list(
        snapshot.position_lines.select_related("security", "account__bank_account__member")
        .filter(account=account)
        .order_by("asset_type", "asset_name", "pk")
    )
    amounts = defaultdict(lambda: ZERO)
    for line in lines:
        amounts[line.asset_type or "unknown"] += line.market_value

    audit = snapshot.extra_data if isinstance(snapshot.extra_data, dict) else {}
    audit_available = "complete" in audit
    complete = audit.get("complete") is True
    warnings = []
    if not audit_available:
        warnings.append("该快照缺少完整性审计标记。")
    if audit.get("missing_exchange_rates"):
        warnings.append("该快照存在缺失汇率。")
    if audit.get("missing_prices"):
        warnings.append("该快照存在缺失价格。")
    if audit.get("stale_prices"):
        warnings.append("该快照存在过期价格。")
    if audit.get("valuation_errors"):
        warnings.append("该快照存在估值错误。")

    percentages = None
    if complete and snapshot.total_asset:
        percentages = {
            key: str((value / snapshot.total_asset * Decimal("100")).quantize(Decimal("0.01")))
            for key, value in sorted(amounts.items())
        }
    return {
        "module": "portfolio",
        "scope": scope,
        "account_id": account.pk,
        "account_name": account.account_name,
        "member_id": account.member_id,
        "member_name": account.member.display_name,
        "snapshot_id": snapshot.pk,
        "valuation_date": snapshot.snapshot_date.isoformat(),
        "currency": snapshot.currency,
        "complete": complete,
        "total_cash": _decimal(snapshot.total_cash),
        "total_market_value": _decimal(snapshot.total_market_value),
        "total_asset": _decimal(snapshot.total_asset),
        "amounts_by_asset_type": {
            key: _decimal(value) for key, value in sorted(amounts.items())
        },
        "percentages_by_asset_type": percentages,
        "warnings": warnings,
        "positions": [
            {
                "asset_type": line.asset_type,
                "asset_name": line.asset_name,
                "security_id": line.security_id,
                "quantity": _decimal(line.quantity),
                "price": _decimal(line.price),
                "price_as_of": line.price_as_of.isoformat() if line.price_as_of else None,
                "price_source": line.price_source,
                "pricing_status": line.pricing_status,
                "currency": line.currency,
                "fx_rate": _decimal(line.fx_rate),
                "fx_rate_as_of": line.fx_rate_as_of.isoformat() if line.fx_rate_as_of else None,
                "market_value_original": _decimal(line.market_value_original),
                "market_value": _decimal(line.market_value),
            }
            for line in lines
        ],
    }
