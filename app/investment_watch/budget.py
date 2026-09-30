from decimal import Decimal, InvalidOperation, ROUND_UP
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from family_core.models import Family
from .models import BudgetReceipt
from .services import WatchError


def amount(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise WatchError("费用配置无效，模型调用已停止。")
    if not result.is_finite() or result < 0:
        raise WatchError("费用配置无效，模型调用已停止。")
    return result


def limits():
    return (
        amount(getattr(settings, "INVESTMENT_WATCH_DAILY_CNY", "1")),
        amount(getattr(settings, "INVESTMENT_WATCH_MONTHLY_CNY", "30")),
    )


def budget_status(family):
    today = timezone.localdate()
    receipts = list(
        BudgetReceipt.objects.filter(
            family=family, created_at__date__gte=today.replace(day=1)
        )
    )

    def charge(r):
        return r.actual_cny if r.actual_cny is not None else r.reserved_cny

    daily = sum(
        (charge(r) for r in receipts if timezone.localdate(r.created_at) == today),
        Decimal(0),
    )
    monthly = sum((charge(r) for r in receipts), Decimal(0))
    daily_limit, monthly_limit = limits()
    return {
        "daily": daily,
        "monthly": monthly,
        "daily_limit": daily_limit,
        "monthly_limit": monthly_limit,
        "collect_enabled": getattr(settings, "INVESTMENT_WATCH_COLLECT_ENABLED", False),
        "model_enabled": getattr(settings, "INVESTMENT_WATCH_MODEL_ENABLED", False),
    }


@transaction.atomic
def reserve(member, provider, key, maximum):
    maximum = amount(maximum).quantize(Decimal(".000001"), rounding=ROUND_UP)
    if maximum <= 0:
        raise WatchError("缺少有效费用估算。")
    Family.objects.select_for_update().get(pk=member.family_id)
    if BudgetReceipt.objects.filter(family=member.family, status="overrun").exists():
        raise WatchError("模型实际用量超过预留，请管理员核对计费配置后再启用。")
    if BudgetReceipt.objects.filter(input_key=key).exists():
        raise WatchError("该输入已有调用记录；不会重复付费。")
    current = budget_status(member.family)
    if (
        current["daily"] + maximum > current["daily_limit"]
        or current["monthly"] + maximum > current["monthly_limit"]
    ):
        raise WatchError("模型额度不足，材料保留为待分析候选。")
    return BudgetReceipt.objects.create(
        family=member.family,
        member=member,
        provider=provider,
        input_key=key,
        reserved_cny=maximum,
    )


@transaction.atomic
def settle(receipt, actual=None, failed=False):
    receipt = BudgetReceipt.objects.select_for_update().get(pk=receipt.pk)
    # Missing/invalid usage and failures remain reserved; never assume a failed request was free.
    if not failed and actual is not None:
        cost = amount(actual)
        receipt.actual_cny = cost.quantize(Decimal(".000001"), rounding=ROUND_UP)
    receipt.status = "failed" if failed else "completed"
    if receipt.actual_cny is not None and receipt.actual_cny > receipt.reserved_cny:
        receipt.status = "overrun"
    receipt.save(update_fields=["actual_cny", "status", "updated_at"])
