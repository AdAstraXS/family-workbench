import calendar
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.db.models import Prefetch
from django.utils import timezone

from family_core.models import FamilyMember

from .models import (
    AssetBalanceEntry,
    AssetBalanceSnapshot,
    InvestmentGoalActualOverride,
    InvestmentGoalPlan,
    InvestmentGoalPoint,
    InvestmentGoalSetting,
)


DEFAULT_START_DATE = date(2025, 12, 31)
DEFAULT_PERIODS = 24
DEFAULT_MEMBER_ASSUMPTIONS = (
    {"semiannual_contribution": Decimal("50000"), "semiannual_return_rate": Decimal("6")},
    {"semiannual_contribution": Decimal("75000"), "semiannual_return_rate": Decimal("3")},
)
MONEY_QUANTUM = Decimal("0.0001")


class InvestmentGoalInitializationError(ValueError):
    pass


def add_months_preserving_month_end(day, months):
    month_index = day.year * 12 + day.month - 1 + months
    year, zero_based_month = divmod(month_index, 12)
    month = zero_based_month + 1
    target_last_day = calendar.monthrange(year, month)[1]
    source_is_month_end = day.day == calendar.monthrange(day.year, day.month)[1]
    target_day = target_last_day if source_is_month_end else min(day.day, target_last_day)
    return date(year, month, target_day)


def goal_date_for_period(start_date, period_index):
    return add_months_preserving_month_end(start_date, period_index * 6)


def calculate_next_target(previous_amount, contribution, return_rate):
    multiplier = Decimal("1") + (return_rate / Decimal("100"))
    return (previous_amount * multiplier + contribution).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


@transaction.atomic
def recalculate_future_goal_points(plan, *, as_of=None):
    as_of = as_of or timezone.localdate()
    start_date = plan.start_snapshot.snapshot_date

    for setting in plan.settings.select_related("member").all():
        initial_point, _ = InvestmentGoalPoint.objects.get_or_create(
            setting=setting,
            period_index=0,
            defaults={
                "target_date": start_date,
                "target_amount": setting.initial_amount,
                "applied_contribution": Decimal("0"),
                "applied_return_rate": Decimal("0"),
                "is_frozen": start_date <= as_of,
            },
        )
        if initial_point.target_date <= as_of and not initial_point.is_frozen:
            initial_point.is_frozen = True
            initial_point.save(update_fields=["is_frozen", "updated_at"])

        InvestmentGoalPoint.objects.filter(
            setting=setting,
            period_index__gt=0,
            target_date__gt=as_of,
        ).delete()
        InvestmentGoalPoint.objects.filter(
            setting=setting,
            target_date__lte=as_of,
            is_frozen=False,
        ).update(is_frozen=True, updated_at=timezone.now())

        anchor = setting.points.filter(target_date__lte=as_of).order_by("-period_index").first()
        if not anchor:
            anchor = initial_point
        previous_amount = anchor.target_amount
        for period_index in range(anchor.period_index + 1, setting.periods + 1):
            target_date = goal_date_for_period(start_date, period_index)
            target_amount = calculate_next_target(
                previous_amount,
                setting.semiannual_contribution,
                setting.semiannual_return_rate,
            )
            point, created = InvestmentGoalPoint.objects.get_or_create(
                setting=setting,
                period_index=period_index,
                defaults={
                    "target_date": target_date,
                    "target_amount": target_amount,
                    "applied_contribution": setting.semiannual_contribution,
                    "applied_return_rate": setting.semiannual_return_rate,
                    "is_frozen": target_date <= as_of,
                },
            )
            if not created and target_date > as_of:
                point.target_date = target_date
                point.target_amount = target_amount
                point.applied_contribution = setting.semiannual_contribution
                point.applied_return_rate = setting.semiannual_return_rate
                point.is_frozen = False
                point.save()
            previous_amount = point.target_amount


@transaction.atomic
def initialize_default_investment_goal_plan(family, *, actor=None, as_of=None):
    existing = (
        InvestmentGoalPlan.objects.filter(family=family, is_active=True)
        .select_related("start_snapshot")
        .prefetch_related("settings__member", "settings__points")
        .first()
    )
    if existing:
        return existing, False

    start_snapshot = (
        AssetBalanceSnapshot.objects.filter(
            family=family,
            snapshot_date=DEFAULT_START_DATE,
            is_draft=False,
        )
        .order_by("-created_at")
        .first()
    )
    if not start_snapshot:
        raise InvestmentGoalInitializationError("缺少 2025-12-31 的正式资产快照，无法建立投资目标。")

    members = list(
        FamilyMember.objects.filter(family=family, is_active=True).order_by("display_order", "id")
    )
    if not members:
        raise InvestmentGoalInitializationError("当前家庭没有可用成员，无法建立投资目标。")

    member_totals = {member.id: Decimal("0") for member in members}
    for member_id, amount in start_snapshot.entries.values_list("member_id", "base_amount"):
        if member_id in member_totals:
            member_totals[member_id] += amount or Decimal("0")

    plan = InvestmentGoalPlan.objects.create(
        family=family,
        name="家庭投资目标",
        start_snapshot=start_snapshot,
        is_active=True,
        remark="以 2025-12-31 正式资产快照为期初，每半年计算一次。",
        created_by=actor,
        updated_by=actor,
    )
    for index, member in enumerate(members):
        defaults = (
            DEFAULT_MEMBER_ASSUMPTIONS[index]
            if index < len(DEFAULT_MEMBER_ASSUMPTIONS)
            else {"semiannual_contribution": Decimal("0"), "semiannual_return_rate": Decimal("0")}
        )
        InvestmentGoalSetting.objects.create(
            plan=plan,
            member=member,
            initial_amount=member_totals[member.id],
            semiannual_contribution=defaults["semiannual_contribution"],
            semiannual_return_rate=defaults["semiannual_return_rate"],
            periods=DEFAULT_PERIODS,
        )
    recalculate_future_goal_points(plan, as_of=as_of)
    return plan, True


def get_goal_actuals(plan, target_dates, members):
    target_dates = set(target_dates)
    member_ids = {member.id for member in members}
    actuals = {}
    snapshots = (
        AssetBalanceSnapshot.objects.filter(
            family=plan.family,
            snapshot_date__in=target_dates,
            is_draft=False,
        )
        .prefetch_related(Prefetch("entries", queryset=AssetBalanceEntry.objects.select_related("member")))
        .order_by("snapshot_date", "created_at")
    )
    for snapshot in snapshots:
        totals = {member_id: Decimal("0") for member_id in member_ids}
        for entry in snapshot.entries.all():
            if entry.member_id in totals:
                totals[entry.member_id] += entry.base_amount or Decimal("0")
        for member_id, amount in totals.items():
            actuals[(snapshot.snapshot_date, member_id)] = {
                "amount": amount,
                "source": "资产快照",
                "snapshot_id": snapshot.id,
            }

    overrides = InvestmentGoalActualOverride.objects.filter(
        plan=plan,
        target_date__in=target_dates,
        member_id__in=member_ids,
    )
    for override in overrides:
        actuals[(override.target_date, override.member_id)] = {
            "amount": override.amount,
            "source": "手工修正",
            "snapshot_id": None,
        }
    return actuals
