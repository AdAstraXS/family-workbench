from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from ledger.models import AssetBalanceEntry, AssetBalanceSnapshot

from .historical_valuation import (
    account_ids_as_of,
    slice_valuation,
    snapshot_exchange_rate,
    value_historical_portfolio,
)
from .models import (
    CashMovementTypeChoices,
    InvestmentAccount,
    InvestmentCashMovement,
    PortfolioReconciliationLine,
    PortfolioReconciliationRun,
    TransactionSourceChoices,
)
from .snapshot_service import create_portfolio_snapshots_for_date


ZERO = Decimal("0")
FOUR_PLACES = Decimal("0.0001")


@dataclass
class ReconciliationPreviewRow:
    account: InvestmentAccount | None
    account_name: str
    member_name: str
    currency: str
    ledger_base_amount: Decimal | None
    calculated_base_amount: Decimal | None
    adjustment_base_amount: Decimal | None
    adjustment_original_amount: Decimal | None
    status: str
    message: str = ""
    existing_movement_id: int | None = None

    @property
    def can_apply(self):
        return self.status == "ready"


@dataclass
class ReconciliationPreview:
    ledger_snapshot: AssetBalanceSnapshot
    rows: list[ReconciliationPreviewRow] = field(default_factory=list)

    @property
    def blocking_rows(self):
        return [item for item in self.rows if item.status == "blocked"]

    @property
    def ready_rows(self):
        return [item for item in self.rows if item.can_apply]

    @property
    def can_apply(self):
        return bool(self.ready_rows) and not self.blocking_rows

    @property
    def total_ledger(self):
        return sum(
            (
                item.ledger_base_amount
                for item in self.ready_rows
                if item.ledger_base_amount is not None
            ),
            ZERO,
        )

    @property
    def total_calculated(self):
        return sum(
            (
                item.calculated_base_amount
                for item in self.ready_rows
                if item.calculated_base_amount is not None
            ),
            ZERO,
        )

    @property
    def total_adjustment(self):
        return sum(
            (
                item.adjustment_base_amount
                for item in self.ready_rows
                if item.adjustment_base_amount is not None
            ),
            ZERO,
        )


def reconciliation_external_id(ledger_snapshot_id, account_id):
    return f"ledger-reconciliation:{ledger_snapshot_id}:{account_id}"


def _ledger_groups(ledger_snapshot):
    groups = {}
    entries = list(
        AssetBalanceEntry.objects.filter(snapshot=ledger_snapshot)
        .select_related(
            "account__investment_profile",
            "account__member",
        )
        .order_by("account_id", "pk")
    )
    unmapped = []
    for entry in entries:
        if not entry.account_id or not entry.account.supports_investment:
            continue
        try:
            account = entry.account.investment_profile
        except InvestmentAccount.DoesNotExist:
            unmapped.append(entry)
            continue
        group = groups.setdefault(
            account.pk,
            {"account": account, "entries": [], "ledger_base": ZERO},
        )
        group["entries"].append(entry)
        group["ledger_base"] += entry.base_amount
    return groups, unmapped


def _adjustment_currency(group, base_currency):
    currencies = {item.currency.upper() for item in group["entries"] if item.currency}
    if len(currencies) == 1:
        return currencies.pop()
    return base_currency.upper()


def build_reconciliation_preview(ledger_snapshot):
    if ledger_snapshot.is_draft:
        raise ValidationError("家庭账本资产快照仍是草稿，不能执行差额对齐。")
    groups, unmapped = _ledger_groups(ledger_snapshot)
    accounts = [item["account"] for item in groups.values()]
    existing_movements = {
        item.account_id: item
        for item in InvestmentCashMovement.objects.filter(
            account__in=accounts,
            source=TransactionSourceChoices.RECONCILIATION,
            external_id__startswith=f"ledger-reconciliation:{ledger_snapshot.pk}:",
        )
    }
    valuation = value_historical_portfolio(
        accounts,
        ledger_snapshot.base_currency,
        ledger_snapshot.snapshot_date,
        ledger_snapshot=ledger_snapshot,
        exclude_movement_ids=[item.pk for item in existing_movements.values()],
    )
    preview = ReconciliationPreview(ledger_snapshot=ledger_snapshot)
    for entry in unmapped:
        preview.rows.append(
            ReconciliationPreviewRow(
                account=None,
                account_name=entry.account.account_name,
                member_name=entry.member.display_name,
                currency=entry.currency,
                ledger_base_amount=entry.base_amount,
                calculated_base_amount=None,
                adjustment_base_amount=None,
                adjustment_original_amount=None,
                status="blocked",
                message="账本账户尚未建立投资账户关联。",
            )
        )

    for group in groups.values():
        account = group["account"]
        account_valuation = slice_valuation(valuation, [account.pk])
        currency = _adjustment_currency(group, ledger_snapshot.base_currency)
        existing = existing_movements.get(account.pk)
        if not account_valuation["complete"]:
            messages = []
            if account_valuation["missing_prices"]:
                messages.append(
                    "缺价："
                    + "、".join(
                        item["security"]
                        for item in account_valuation["missing_prices"]
                    )
                )
            if account_valuation["missing_rates"]:
                messages.append(
                    "缺汇率："
                    + "、".join(
                        sorted(
                            {
                                item["currency"]
                                for item in account_valuation["missing_rates"]
                            }
                        )
                    )
                )
            if account_valuation["errors"]:
                messages.append(
                    "流水错误："
                    + "；".join(
                        item["message"] for item in account_valuation["errors"]
                    )
                )
            preview.rows.append(
                ReconciliationPreviewRow(
                    account=account,
                    account_name=account.account_name,
                    member_name=account.member.display_name,
                    currency=currency,
                    ledger_base_amount=group["ledger_base"],
                    calculated_base_amount=account_valuation["total_asset"],
                    adjustment_base_amount=None,
                    adjustment_original_amount=None,
                    status="blocked",
                    message="；".join(messages),
                    existing_movement_id=existing.pk if existing else None,
                )
            )
            continue
        if not currency:
            preview.rows.append(
                ReconciliationPreviewRow(
                    account=account,
                    account_name=account.account_name,
                    member_name=account.member.display_name,
                    currency="",
                    ledger_base_amount=group["ledger_base"],
                    calculated_base_amount=account_valuation["total_asset"],
                    adjustment_base_amount=None,
                    adjustment_original_amount=None,
                    status="blocked",
                    message="该账户存在多个原币，且账户默认币种为空。",
                    existing_movement_id=existing.pk if existing else None,
                )
            )
            continue
        rate = snapshot_exchange_rate(
            currency,
            ledger_snapshot.base_currency,
            ledger_snapshot.snapshot_date,
            ledger_snapshot,
        )
        if not rate:
            preview.rows.append(
                ReconciliationPreviewRow(
                    account=account,
                    account_name=account.account_name,
                    member_name=account.member.display_name,
                    currency=currency,
                    ledger_base_amount=group["ledger_base"],
                    calculated_base_amount=account_valuation["total_asset"],
                    adjustment_base_amount=None,
                    adjustment_original_amount=None,
                    status="blocked",
                    message=f"缺少 {currency}/{ledger_snapshot.base_currency} 汇率。",
                    existing_movement_id=existing.pk if existing else None,
                )
            )
            continue
        difference = group["ledger_base"] - account_valuation["total_asset"]
        preview.rows.append(
            ReconciliationPreviewRow(
                account=account,
                account_name=account.account_name,
                member_name=account.member.display_name,
                currency=currency,
                ledger_base_amount=group["ledger_base"],
                calculated_base_amount=account_valuation["total_asset"],
                adjustment_base_amount=difference,
                adjustment_original_amount=(difference / rate).quantize(
                    FOUR_PLACES, rounding=ROUND_HALF_UP
                ),
                status="ready",
                message="将更新原调整流水" if existing else "",
                existing_movement_id=existing.pk if existing else None,
            )
        )
    preview.rows.sort(key=lambda item: (item.member_name, item.account_name))
    return preview


def _report(preview):
    return {
        "snapshot_date": preview.ledger_snapshot.snapshot_date.isoformat(),
        "total_ledger": str(preview.total_ledger),
        "total_calculated_before": str(preview.total_calculated),
        "total_adjustment": str(preview.total_adjustment),
        "accounts": [
            {
                "account_id": row.account.pk if row.account else None,
                "account": row.account_name,
                "member": row.member_name,
                "currency": row.currency,
                "ledger_base": str(row.ledger_base_amount),
                "calculated_base": str(row.calculated_base_amount),
                "adjustment_base": str(row.adjustment_base_amount),
                "adjustment_original": str(row.adjustment_original_amount),
                "status": row.status,
                "message": row.message,
            }
            for row in preview.rows
        ],
    }


@transaction.atomic
def apply_reconciliation(ledger_snapshot, actor=None):
    later_run = PortfolioReconciliationRun.objects.filter(
        family=ledger_snapshot.family,
        ledger_snapshot__snapshot_date__gt=ledger_snapshot.snapshot_date,
        status=PortfolioReconciliationRun.STATUS_APPLIED,
    ).exists()
    if later_run:
        raise ValidationError("已有更晚月份的对齐记录，请先撤销更晚月份后再修改本期。")
    preview = build_reconciliation_preview(ledger_snapshot)
    if not preview.can_apply:
        raise ValidationError("差额预览存在缺价、缺汇率或账户映射错误，尚不能执行。")

    now = timezone.now()
    run, _created = PortfolioReconciliationRun.objects.update_or_create(
        ledger_snapshot=ledger_snapshot,
        defaults={
            "family": ledger_snapshot.family,
            "base_currency": ledger_snapshot.base_currency,
            "status": PortfolioReconciliationRun.STATUS_APPLIED,
            "applied_by": actor,
            "applied_at": now,
            "reverted_by": None,
            "reverted_at": None,
            "report": _report(preview),
        },
    )
    run.lines.all().delete()
    for row in preview.ready_rows:
        external_id = reconciliation_external_id(
            ledger_snapshot.pk, row.account.pk
        )
        movement = None
        if row.adjustment_original_amount:
            movement, _created = InvestmentCashMovement.objects.update_or_create(
                account=row.account,
                source=TransactionSourceChoices.RECONCILIATION,
                external_id=external_id,
                defaults={
                    "transaction": None,
                    "movement_date": ledger_snapshot.snapshot_date,
                    "settlement_date": ledger_snapshot.snapshot_date,
                    "movement_type": CashMovementTypeChoices.ADJUSTMENT,
                    "currency": row.currency,
                    "amount": row.adjustment_original_amount,
                    "created_by": actor,
                    "updated_by": actor,
                    "remark": (
                        f"{ledger_snapshot.snapshot_date} 家庭账本月底资产差额对齐；"
                        f"调整前 {row.calculated_base_amount} {ledger_snapshot.base_currency}，"
                        f"账本 {row.ledger_base_amount} {ledger_snapshot.base_currency}。"
                    ),
                },
            )
        else:
            InvestmentCashMovement.objects.filter(
                account=row.account,
                source=TransactionSourceChoices.RECONCILIATION,
                external_id=external_id,
            ).delete()
        PortfolioReconciliationLine.objects.create(
            run=run,
            account=row.account,
            currency=row.currency,
            ledger_base_amount=row.ledger_base_amount,
            calculated_base_amount=row.calculated_base_amount,
            adjustment_base_amount=row.adjustment_base_amount,
            adjustment_original_amount=row.adjustment_original_amount,
            movement=movement,
        )

    accounts = list(
        InvestmentAccount.objects.filter(
            pk__in=account_ids_as_of(
                ledger_snapshot.family, ledger_snapshot.snapshot_date
            )
        )
        .select_related("bank_account__member")
    )
    create_portfolio_snapshots_for_date(
        ledger_snapshot.family,
        accounts,
        ledger_snapshot.snapshot_date,
        ledger_snapshot.base_currency,
        require_complete=True,
    )
    return run


@transaction.atomic
def revert_reconciliation(run, actor=None):
    later_run = PortfolioReconciliationRun.objects.filter(
        family=run.family,
        ledger_snapshot__snapshot_date__gt=run.ledger_snapshot.snapshot_date,
        status=PortfolioReconciliationRun.STATUS_APPLIED,
    ).exists()
    if later_run:
        raise ValidationError("请先撤销更晚月份的差额对齐。")
    movement_ids = list(
        run.lines.exclude(movement=None).values_list("movement_id", flat=True)
    )
    InvestmentCashMovement.objects.filter(pk__in=movement_ids).delete()
    run.status = PortfolioReconciliationRun.STATUS_REVERTED
    run.reverted_by = actor
    run.reverted_at = timezone.now()
    run.save(update_fields=["status", "reverted_by", "reverted_at", "updated_at"])

    accounts = list(
        InvestmentAccount.objects.filter(
            pk__in=account_ids_as_of(
                run.family, run.ledger_snapshot.snapshot_date
            )
        )
        .select_related("bank_account__member")
    )
    create_portfolio_snapshots_for_date(
        run.family,
        accounts,
        run.ledger_snapshot.snapshot_date,
        run.base_currency,
    )
    return run
