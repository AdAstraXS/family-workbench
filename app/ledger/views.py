from django.contrib.auth.decorators import login_required
from django.db.models import Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from decimal import Decimal
import json

from .forms import (
    AnnualBudgetForm,
    AnnualBudgetLineFormSet,
    AssetBalanceEntryFormSet,
    AssetBalanceSnapshotForm,
    BankAccountForm,
    ExpenseCategoryForm,
    ExpenseRecordForm,
    IncomeCategoryForm,
    IncomeRecordForm,
    make_annual_budget_line_formset,
    make_asset_balance_entry_formset,
)
from .models import AnnualBudget, AnnualBudgetLine, AssetBalanceSnapshot, BankAccount, ExpenseCategory, ExpenseRecord, IncomeCategory, IncomeRecord
from family_core.models import Family, FamilyMember


def save_form(request, form_class, template_name, success_url_name, title, instance=None):
    request_aware_forms = (BankAccountForm, IncomeRecordForm, ExpenseRecordForm)
    if request.method == "POST":
        form = form_class(request.POST, instance=instance, request=request) if form_class in request_aware_forms else form_class(request.POST, instance=instance)
        if form.is_valid():
            obj = form.save()
            if isinstance(obj, BankAccount):
                request.session["last_account_member_id"] = obj.member_id
            elif isinstance(obj, IncomeRecord):
                request.session["last_income_member_id"] = obj.member_id
            elif isinstance(obj, ExpenseRecord):
                request.session["last_expense_member_id"] = obj.member_id
            return redirect(success_url_name)
    else:
        form = form_class(instance=instance, request=request) if form_class in request_aware_forms else form_class(instance=instance)
    return render(request, template_name, {"form": form, "title": title})


def current_month_record_filter(date_field, period_start_field, period_end_field):
    today = timezone.localdate()
    month_start = today.replace(day=1)
    return (
        Q(**{f"{period_start_field}__lte": today, f"{period_end_field}__gte": month_start})
        | Q(**{f"{period_start_field}__isnull": True, f"{date_field}__year": today.year, f"{date_field}__month": today.month})
    )


def get_record_month(record, fallback_field):
    record_date = record.period_start or getattr(record, fallback_field)
    return record_date.year, record_date.month


def get_record_year(record, fallback_field):
    record_date = record.period_start or getattr(record, fallback_field)
    return record_date.year


def get_record_year_month_label(record, fallback_field):
    record_date = record.period_start or getattr(record, fallback_field)
    return f"{record_date.year}年{record_date.month}月"


def build_cashflow_monthly_rows():
    default_family = Family.objects.filter(name="我的家庭").first() or Family.objects.first()
    members_query = FamilyMember.objects.filter(is_active=True)
    if default_family:
        members_query = members_query.filter(family=default_family)
    members = list(members_query.order_by("display_name"))
    member_ids = [member.id for member in members]

    income_records = IncomeRecord.objects.select_related("member").all()
    expense_records = ExpenseRecord.objects.select_related("member").all()
    if default_family:
        income_records = income_records.filter(family=default_family)
        expense_records = expense_records.filter(family=default_family)

    month_map = {}
    years = set()

    def ensure_month(year, month):
        years.add(year)
        key = (year, month)
        if key not in month_map:
            month_map[key] = {
                "year": year,
                "month": month,
                "label": f"{year}年{month}月",
                "income": {member_id: Decimal("0") for member_id in member_ids},
                "expense": {member_id: Decimal("0") for member_id in member_ids},
            }
        return month_map[key]

    for record in income_records:
        if record.member_id not in member_ids:
            continue
        year, month = get_record_month(record, "income_date")
        ensure_month(year, month)["income"][record.member_id] += record.amount or Decimal("0")

    for record in expense_records:
        if record.member_id not in member_ids:
            continue
        year, month = get_record_month(record, "expense_date")
        ensure_month(year, month)["expense"][record.member_id] += record.amount or Decimal("0")

    today = timezone.localdate()
    if not month_map:
        ensure_month(today.year, today.month)

    rows = []
    year_totals = {}
    for key in sorted(month_map.keys()):
        row = month_map[key]
        row["income_cells"] = [row["income"][member.id] for member in members]
        row["expense_cells"] = [row["expense"][member.id] for member in members]
        row["net_cells"] = [row["income"][member.id] - row["expense"][member.id] for member in members]
        rows.append(row)

        year_total = year_totals.setdefault(
            row["year"],
            {
                "label": f"{row['year']}年合计",
                "income": {member_id: Decimal("0") for member_id in member_ids},
                "expense": {member_id: Decimal("0") for member_id in member_ids},
            },
        )
        for member_id in member_ids:
            year_total["income"][member_id] += row["income"][member_id]
            year_total["expense"][member_id] += row["expense"][member_id]

    for year_total in year_totals.values():
        year_total["income_cells"] = [year_total["income"][member.id] for member in members]
        year_total["expense_cells"] = [year_total["expense"][member.id] for member in members]
        year_total["net_cells"] = [
            year_total["income"][member.id] - year_total["expense"][member.id]
            for member in members
        ]

    sections = []
    for year in sorted(year_totals.keys()):
        sections.append(
            {
                "year": year,
                "rows": [row for row in rows if row["year"] == year],
                "total": year_totals[year],
            }
        )

    return members, sections


def get_default_family_records():
    default_family = Family.objects.filter(name="我的家庭").first() or Family.objects.first()
    income_records = IncomeRecord.objects.select_related("family", "member", "category", "category__parent")
    expense_records = ExpenseRecord.objects.select_related("family", "member", "category", "category__parent")
    if default_family:
        income_records = income_records.filter(family=default_family)
        expense_records = expense_records.filter(family=default_family)
    return default_family, income_records, expense_records


def build_annual_cashflow_rows():
    default_family, income_records, expense_records = get_default_family_records()
    members = list(FamilyMember.objects.filter(family=default_family, is_active=True).order_by("display_name")) if default_family else []
    member_ids = [member.id for member in members]
    year_map = {}

    def ensure_year(family, year):
        key = (family.id if family else None, year)
        if key not in year_map:
            year_map[key] = {
                "family": family,
                "year": year,
                "income": {member_id: Decimal("0") for member_id in member_ids},
                "expense": {member_id: Decimal("0") for member_id in member_ids},
            }
        return year_map[key]

    for record in income_records:
        year = get_record_year(record, "income_date")
        row = ensure_year(record.family, year)
        if record.member_id in row["income"]:
            row["income"][record.member_id] += record.amount or Decimal("0")

    for record in expense_records:
        year = get_record_year(record, "expense_date")
        row = ensure_year(record.family, year)
        if record.member_id in row["expense"]:
            row["expense"][record.member_id] += record.amount or Decimal("0")

    rows = []
    for row in year_map.values():
        income_cells = [row["income"][member.id] for member in members]
        expense_cells = [row["expense"][member.id] for member in members]
        net_cells = [row["income"][member.id] - row["expense"][member.id] for member in members]
        rows.append(
            {
                "family": row["family"],
                "year": row["year"],
                "income_cells": income_cells,
                "income_total": sum(income_cells, Decimal("0")),
                "expense_cells": expense_cells,
                "expense_total": sum(expense_cells, Decimal("0")),
                "net_cells": net_cells,
                "net_total": sum(net_cells, Decimal("0")),
            }
        )
    rows.sort(key=lambda item: (item["year"], str(item["family"])), reverse=True)
    return members, rows


def build_year_cashflow_detail(year):
    default_family, income_records, expense_records = get_default_family_records()
    members = list(FamilyMember.objects.filter(family=default_family, is_active=True).order_by("display_name")) if default_family else []
    member_ids = [member.id for member in members]
    income_records = list(
        income_records.filter(Q(period_start__year=year) | Q(period_start__isnull=True, income_date__year=year))
        .order_by("period_start", "income_date", "member__display_name", "created_at")
    )
    expense_records = list(
        expense_records.filter(Q(period_start__year=year) | Q(period_start__isnull=True, expense_date__year=year))
        .order_by("period_start", "expense_date", "member__display_name", "created_at")
    )

    def make_row(record, kind):
        fallback_field = "income_date" if kind == "income" else "expense_date"
        return {
            "kind": kind,
            "record": record,
            "period_label": get_record_year_month_label(record, fallback_field),
            "sort_key": record.period_start or getattr(record, fallback_field),
            "member": record.member,
            "category": record.category,
            "amount": record.amount,
            "currency": record.currency,
            "remark": record.remark,
        }

    income_rows = [make_row(record, "income") for record in income_records]
    expense_rows = [make_row(record, "expense") for record in expense_records]

    income_member_totals = {member_id: Decimal("0") for member_id in member_ids}
    expense_member_totals = {member_id: Decimal("0") for member_id in member_ids}
    for record in income_records:
        if record.member_id in income_member_totals:
            income_member_totals[record.member_id] += record.amount or Decimal("0")
    for record in expense_records:
        if record.member_id in expense_member_totals:
            expense_member_totals[record.member_id] += record.amount or Decimal("0")

    return {
        "family": default_family,
        "members": members,
        "income_rows": income_rows,
        "expense_rows": expense_rows,
        "income_member_totals": [{"member": member, "amount": income_member_totals[member.id]} for member in members],
        "expense_member_totals": [{"member": member, "amount": expense_member_totals[member.id]} for member in members],
        "income_family_total": sum(income_member_totals.values(), Decimal("0")),
        "expense_family_total": sum(expense_member_totals.values(), Decimal("0")),
    }


def calculate_base_amount(snapshot, currency, original_amount):
    amount = original_amount or Decimal("0")
    if currency == snapshot.base_currency:
        return amount
    if currency == "USD" and snapshot.usd_to_base:
        return amount * snapshot.usd_to_base
    if currency == "HKD" and snapshot.hkd_to_base:
        return amount * snapshot.hkd_to_base
    return amount


def get_period_month(record, fallback_field):
    record_date = record.period_start or getattr(record, fallback_field)
    return record_date.month


BUDGET_CATEGORY_ORDER = [
    ("income", "工资"),
    ("income", "收入-工资"),
    ("income", "奖金"),
    ("income", "收入-奖金"),
    ("expense", "经营性-餐饮"),
    ("expense", "经营性-日用百货"),
    ("expense", "经营性-母婴花费"),
    ("expense", "经营性-美容美发"),
    ("expense", "经营性-物业车位"),
    ("expense", "经营性-水电燃气话费"),
    ("expense", "经营性-加油费"),
    ("expense", "经营性-车辆保养"),
    ("expense", "经营性-保险"),
    ("expense", "经营性-医疗保健"),
    ("expense", "经营性-知识付费、买书、会员卡"),
    ("expense", "经营性-模型、游戏"),
    ("expense", "经营性-旅游/交通出行"),
    ("expense", "经营性-服饰包包"),
    ("expense", "固定资产-电器、数码产品"),
    ("expense", "固定资产-新车分期"),
    ("expense", "营业外-BVI"),
    ("expense", "营业外-人情世故礼物等"),
    ("expense", "营业外-其他"),
]
BUDGET_CATEGORY_ORDER_MAP = {item: index for index, item in enumerate(BUDGET_CATEGORY_ORDER)}


def budget_category_label(line):
    category = line.income_category if line.line_type == AnnualBudgetLine.LINE_TYPE_INCOME else line.expense_category
    return str(category) if category else ""


def budget_line_sort_key(row):
    line = row["line"]
    label = budget_category_label(line)
    key = (line.line_type, label)
    if key in BUDGET_CATEGORY_ORDER_MAP:
        return (BUDGET_CATEGORY_ORDER_MAP[key], label)
    fallback_group = 100 if line.line_type == AnnualBudgetLine.LINE_TYPE_INCOME else 200
    return (fallback_group, label)


def build_budget_total_row(label, rows, line_type):
    budget_amount = sum((row["budget"] for row in rows), Decimal("0"))
    actual = sum((row["actual"] for row in rows), Decimal("0"))
    variance = actual - budget_amount
    execution_rate = (actual / budget_amount * Decimal("100")) if budget_amount else None
    return {
        "label": label,
        "line_type": line_type,
        "budget": budget_amount,
        "monthly_budget": budget_amount / Decimal("12"),
        "actual": actual,
        "variance": variance,
        "execution_rate": execution_rate,
    }


def expense_budget_group(row):
    category = row["category"]
    if not category:
        return "其他"
    if category.parent:
        return category.parent.name
    label = str(category)
    if "-" in label:
        return label.split("-", 1)[0]
    return label


def get_latest_budget_line_initial():
    latest_budget = AnnualBudget.objects.prefetch_related("lines").order_by("-year", "-created_at").first()
    if not latest_budget:
        return [{} for _ in range(8)]
    latest_lines = list(
        latest_budget.lines.select_related(
            "income_category",
            "income_category__parent",
            "expense_category",
            "expense_category__parent",
        )
    )
    latest_lines.sort(
        key=lambda line: BUDGET_CATEGORY_ORDER_MAP.get((line.line_type, budget_category_label(line)), 999)
    )
    initial_rows = []
    for line in latest_lines:
        initial_rows.append(
            {
                "line_type": line.line_type,
                "income_category": line.income_category_id,
                "expense_category": line.expense_category_id,
                "annual_amount": None,
                "remark": line.remark,
            }
        )
    return initial_rows or [{} for _ in range(8)]


def get_budget_actual_records(budget):
    income_records = IncomeRecord.objects.filter(family=budget.family).select_related("member", "category", "category__parent")
    expense_records = ExpenseRecord.objects.filter(family=budget.family).select_related("member", "category", "category__parent")
    income_records = income_records.filter(
        Q(period_start__year=budget.year) | Q(period_start__isnull=True, income_date__year=budget.year)
    )
    expense_records = expense_records.filter(
        Q(period_start__year=budget.year) | Q(period_start__isnull=True, expense_date__year=budget.year)
    )
    return income_records, expense_records


def build_budget_report(budget):
    lines = list(
        budget.lines.select_related(
            "income_category",
            "income_category__parent",
            "expense_category",
            "expense_category__parent",
        )
    )
    income_records, expense_records = get_budget_actual_records(budget)
    income_records = list(income_records)
    expense_records = list(expense_records)

    total_income_budget = Decimal("0")
    total_expense_budget = Decimal("0")
    total_income_actual = Decimal("0")
    total_expense_actual = Decimal("0")
    line_rows = []

    for line in lines:
        actual = Decimal("0")
        if line.line_type == AnnualBudgetLine.LINE_TYPE_INCOME:
            total_income_budget += line.annual_amount or Decimal("0")
            for record in income_records:
                if line.income_category_id and record.category_id != line.income_category_id:
                    continue
                actual += record.amount or Decimal("0")
        else:
            total_expense_budget += line.annual_amount or Decimal("0")
            for record in expense_records:
                if line.expense_category_id and record.category_id != line.expense_category_id:
                    continue
                actual += record.amount or Decimal("0")

        budget_amount = line.annual_amount or Decimal("0")
        variance = actual - budget_amount
        execution_rate = (actual / budget_amount * Decimal("100")) if budget_amount else None
        category = line.income_category if line.line_type == AnnualBudgetLine.LINE_TYPE_INCOME else line.expense_category
        line_rows.append(
            {
                "line": line,
                "type_label": "收入" if line.line_type == AnnualBudgetLine.LINE_TYPE_INCOME else "支出",
                "category": category,
                "budget": budget_amount,
                "monthly_budget": budget_amount / Decimal("12"),
                "actual": actual,
                "variance": variance,
                "execution_rate": execution_rate,
            }
        )

    line_rows.sort(key=budget_line_sort_key)
    income_line_rows = [row for row in line_rows if row["line"].line_type == AnnualBudgetLine.LINE_TYPE_INCOME]
    expense_line_rows = [row for row in line_rows if row["line"].line_type == AnnualBudgetLine.LINE_TYPE_EXPENSE]
    expense_summary_rows = []
    for group_name in ["经营性", "固定资产", "营业外"]:
        group_rows = [row for row in expense_line_rows if expense_budget_group(row) == group_name]
        expense_summary_rows.append(build_budget_total_row(f"{group_name}汇总", group_rows, AnnualBudgetLine.LINE_TYPE_EXPENSE))
    expense_summary_rows.append(build_budget_total_row("支出汇总", expense_line_rows, AnnualBudgetLine.LINE_TYPE_EXPENSE))

    for record in income_records:
        total_income_actual += record.amount or Decimal("0")
    for record in expense_records:
        total_expense_actual += record.amount or Decimal("0")

    month_rows = []
    for month in range(1, 13):
        income_actual = sum(
            (record.amount or Decimal("0"))
            for record in income_records
            if get_period_month(record, "income_date") == month
        )
        expense_actual = sum(
            (record.amount or Decimal("0"))
            for record in expense_records
            if get_period_month(record, "expense_date") == month
        )
        income_budget = total_income_budget / Decimal("12")
        expense_budget = total_expense_budget / Decimal("12")
        month_rows.append(
            {
                "label": f"{budget.year}年{month}月",
                "income_budget": income_budget,
                "income_actual": income_actual,
                "income_variance": income_actual - income_budget,
                "expense_budget": expense_budget,
                "expense_actual": expense_actual,
                "expense_variance": expense_actual - expense_budget,
                "net_budget": income_budget - expense_budget,
                "net_actual": income_actual - expense_actual,
            }
        )

    return {
        "line_rows": line_rows,
        "income_line_rows": income_line_rows,
        "income_summary_row": build_budget_total_row("收入汇总", income_line_rows, AnnualBudgetLine.LINE_TYPE_INCOME),
        "expense_line_rows": expense_line_rows,
        "expense_summary_rows": expense_summary_rows,
        "month_rows": month_rows,
        "summary": {
            "income_budget": total_income_budget,
            "income_actual": total_income_actual,
            "income_variance": total_income_actual - total_income_budget,
            "expense_budget": total_expense_budget,
            "expense_actual": total_expense_actual,
            "expense_variance": total_expense_actual - total_expense_budget,
            "net_budget": total_income_budget - total_expense_budget,
            "net_actual": total_income_actual - total_expense_actual,
            "net_variance": (total_income_actual - total_expense_actual) - (total_income_budget - total_expense_budget),
        },
    }


def save_budget_formset(formset, budget):
    for form in formset.forms:
        if not form.cleaned_data:
            continue
        if form.cleaned_data.get("DELETE"):
            if form.instance.pk:
                form.instance.delete()
            continue
        line = form.save(commit=False)
        line.budget = budget
        if line.line_type == AnnualBudgetLine.LINE_TYPE_INCOME:
            line.expense_category = None
        else:
            line.income_category = None
        line.save()


def save_asset_snapshot_formset(formset, snapshot):
    saved_ids = set()
    order = 1
    for form in formset.forms:
        if not form.cleaned_data:
            continue
        if form.cleaned_data.get("DELETE"):
            if form.instance.pk:
                form.instance.delete()
            continue
        member = form.cleaned_data.get("member")
        account = form.cleaned_data.get("account")
        asset_category = form.cleaned_data.get("asset_category")
        original_amount = form.cleaned_data.get("original_amount")
        if not any([member, account, asset_category, original_amount]):
            continue
        entry = form.instance if form.instance.pk else form.save(commit=False)
        entry.snapshot = snapshot
        entry.display_order = order
        if entry.account:
            entry.account_name = entry.account.account_name
        entry.base_amount = calculate_base_amount(snapshot, entry.currency, entry.original_amount)
        entry.save()
        saved_ids.add(entry.pk)
        order += 1
    if snapshot.pk and saved_ids:
        snapshot.entries.exclude(pk__in=saved_ids).delete()


def get_account_member_map():
    accounts = BankAccount.objects.filter(is_active=True).values("id", "member_id")
    return json.dumps({str(account["id"]): str(account["member_id"]) for account in accounts})


def get_account_options():
    accounts = BankAccount.objects.filter(is_active=True).select_related("member").order_by("member__display_name", "account_name")
    return json.dumps(
        [
            {
                "id": str(account.id),
                "member_id": str(account.member_id),
                "label": str(account),
            }
            for account in accounts
        ]
    )


def get_latest_snapshot_entry_initial():
    latest_snapshot = AssetBalanceSnapshot.objects.order_by("-snapshot_date", "-created_at").first()
    if not latest_snapshot:
        return [{}]
    return [
        {
            "member": entry.member_id,
            "account": entry.account_id,
            "asset_category": entry.asset_category_id,
            "currency": entry.currency,
            "original_amount": None,
            "remark": entry.remark,
        }
        for entry in latest_snapshot.entries.order_by("display_order", "account__account_name", "asset_category__name")
    ] or [{}]


def build_asset_snapshot_matrix(snapshot):
    entries = snapshot.entries.select_related("member", "account", "asset_category").order_by("display_order", "account__account_name", "asset_category__name", "currency", "member__display_name")
    members = []
    member_ids = set()
    rows = {}
    currency_codes = ["CNY", "USD", "HKD"]
    currency_labels = {"CNY": "人民币合计", "USD": "美元合计", "HKD": "港币合计"}
    currency_totals = {
        code: {
            "label": currency_labels[code],
            "members": {},
            "total_original": Decimal("0"),
            "total_base": Decimal("0"),
        }
        for code in currency_codes
    }
    for entry in entries:
        account_name = entry.account.account_name if entry.account else entry.account_name
        asset_category_name = entry.asset_category.name if entry.asset_category else ""
        original_amount = entry.original_amount or Decimal("0")
        base_amount = entry.base_amount or Decimal("0")
        if entry.member_id not in member_ids:
            members.append(entry.member)
            member_ids.add(entry.member_id)
        key = (account_name, asset_category_name, entry.currency)
        if key not in rows:
            rows[key] = {
                "account_name": account_name,
                "asset_category": asset_category_name,
                "currency": entry.currency,
                "members": {},
                "total_original": Decimal("0"),
                "total_base": Decimal("0"),
                "remark": entry.remark,
            }
        rows[key]["members"][entry.member_id] = entry
        rows[key]["total_original"] += original_amount
        rows[key]["total_base"] += base_amount
        if entry.currency not in currency_totals:
            currency_totals[entry.currency] = {
                "label": f"{entry.currency}合计",
                "members": {},
                "total_original": Decimal("0"),
                "total_base": Decimal("0"),
            }
        member_total = currency_totals[entry.currency]["members"].setdefault(
            entry.member_id,
            {"original": Decimal("0"), "base": Decimal("0")},
        )
        member_total["original"] += original_amount
        member_total["base"] += base_amount
        currency_totals[entry.currency]["total_original"] += original_amount
        currency_totals[entry.currency]["total_base"] += base_amount
    for row in rows.values():
        row["cells"] = [row["members"].get(member.id) for member in members]
    currency_total_rows = []
    member_base_totals = {member.id: Decimal("0") for member in members}
    for code, total in currency_totals.items():
        total["cells"] = [total["members"].get(member.id, {"original": Decimal("0"), "base": Decimal("0")}) for member in members]
        currency_total_rows.append(total)
        for member in members:
            member_base_totals[member.id] += total["members"].get(member.id, {}).get("base", Decimal("0"))
    grand_total = sum((row["total_base"] for row in rows.values()), Decimal("0"))
    base_grand_total = sum(member_base_totals.values(), Decimal("0"))
    base_total_row = {
        "label": "本位币总计",
        "cells": [member_base_totals[member.id] for member in members],
        "total": base_grand_total,
    }
    return members, rows.values(), currency_total_rows, base_total_row, grand_total


@login_required
def overview(request):
    latest_snapshot = AssetBalanceSnapshot.objects.order_by("-snapshot_date", "-created_at").first()
    bank_total = latest_snapshot.entries.aggregate(total=Sum("base_amount"))["total"] if latest_snapshot else 0
    bank_total = bank_total or 0
    month_income = IncomeRecord.objects.filter(
        current_month_record_filter("income_date", "period_start", "period_end")
    ).aggregate(total=Sum("amount"))["total"] or 0
    month_expense = ExpenseRecord.objects.filter(
        current_month_record_filter("expense_date", "period_start", "period_end")
    ).aggregate(total=Sum("amount"))["total"] or 0
    return render(
        request,
        "ledger/overview.html",
        {
            "bank_total": bank_total,
            "month_income": month_income,
            "month_expense": month_expense,
            "month_net": month_income - month_expense,
            "latest_snapshot": latest_snapshot,
        },
    )


@login_required
def annual_budget_list(request):
    budgets = AnnualBudget.objects.select_related("family").order_by("-year", "family__name")
    rows = []
    for budget in budgets:
        report = build_budget_report(budget)
        summary = report["summary"]
        income_rate = (
            summary["income_actual"] / summary["income_budget"] * Decimal("100")
            if summary["income_budget"]
            else None
        )
        expense_rate = (
            summary["expense_actual"] / summary["expense_budget"] * Decimal("100")
            if summary["expense_budget"]
            else None
        )
        rows.append(
            {
                "budget": budget,
                "income_budget": summary["income_budget"],
                "income_actual": summary["income_actual"],
                "income_rate": income_rate,
                "expense_budget": summary["expense_budget"],
                "expense_actual": summary["expense_actual"],
                "expense_rate": expense_rate,
            }
        )
    return render(request, "ledger/annual_budget_list.html", {"rows": rows})


@login_required
def annual_budget_detail(request, pk):
    budget = get_object_or_404(AnnualBudget.objects.select_related("family"), pk=pk)
    report = build_budget_report(budget)
    return render(request, "ledger/annual_budget_detail.html", {"budget": budget, "report": report})


@login_required
def annual_budget_create(request):
    budget = AnnualBudget()
    if request.method == "POST":
        form = AnnualBudgetForm(request.POST, instance=budget)
        formset = AnnualBudgetLineFormSet(request.POST, instance=budget)
        if form.is_valid() and formset.is_valid():
            budget = form.save()
            formset = AnnualBudgetLineFormSet(request.POST, instance=budget)
            if formset.is_valid():
                save_budget_formset(formset, budget)
                return redirect("ledger:annual_budget_detail", pk=budget.pk)
    else:
        initial_lines = get_latest_budget_line_initial()
        InitialAnnualBudgetLineFormSet = make_annual_budget_line_formset(extra=len(initial_lines))
        form = AnnualBudgetForm(instance=budget)
        formset = InitialAnnualBudgetLineFormSet(instance=budget, initial=initial_lines)
    return render(request, "ledger/annual_budget_form.html", {"form": form, "formset": formset, "title": "新增年度预算"})


@login_required
def annual_budget_edit(request, pk):
    budget = get_object_or_404(AnnualBudget, pk=pk)
    if request.method == "POST":
        form = AnnualBudgetForm(request.POST, instance=budget)
        formset = AnnualBudgetLineFormSet(request.POST, instance=budget)
        if form.is_valid() and formset.is_valid():
            budget = form.save()
            save_budget_formset(formset, budget)
            return redirect("ledger:annual_budget_detail", pk=budget.pk)
    else:
        form = AnnualBudgetForm(instance=budget)
        formset = AnnualBudgetLineFormSet(instance=budget)
    return render(request, "ledger/annual_budget_form.html", {"form": form, "formset": formset, "title": "编辑年度预算"})


@login_required
def asset_snapshot_list(request):
    snapshots = list(
        AssetBalanceSnapshot.objects.select_related("family")
        .prefetch_related("entries__member")
        .order_by("-snapshot_date", "-created_at")
    )
    family_ids = {snapshot.family_id for snapshot in snapshots if snapshot.family_id}
    members = list(FamilyMember.objects.filter(family_id__in=family_ids, is_active=True).order_by("display_name"))
    rows = []
    for snapshot in snapshots:
        member_totals = {member.id: Decimal("0") for member in members}
        total = Decimal("0")
        for entry in snapshot.entries.all():
            amount = entry.base_amount or Decimal("0")
            total += amount
            if entry.member_id in member_totals:
                member_totals[entry.member_id] += amount
        rows.append(
            {
                "snapshot": snapshot,
                "member_totals": [member_totals[member.id] for member in members],
                "total": total,
            }
        )
    return render(request, "ledger/asset_snapshot_list.html", {"rows": rows, "members": members})


@login_required
def asset_snapshot_detail(request, pk):
    snapshot = get_object_or_404(AssetBalanceSnapshot.objects.select_related("family"), pk=pk)
    members, rows, currency_totals, base_total_row, grand_total = build_asset_snapshot_matrix(snapshot)
    return render(
        request,
        "ledger/asset_snapshot_detail.html",
        {
            "snapshot": snapshot,
            "members": members,
            "rows": rows,
            "currency_totals": currency_totals,
            "base_total_row": base_total_row,
            "grand_total": grand_total,
        },
    )


@login_required
def asset_snapshot_create(request):
    snapshot = AssetBalanceSnapshot()
    if request.method == "POST":
        form = AssetBalanceSnapshotForm(request.POST, instance=snapshot)
        formset = AssetBalanceEntryFormSet(request.POST, instance=snapshot)
        if form.is_valid() and formset.is_valid():
            snapshot = form.save()
            formset = AssetBalanceEntryFormSet(request.POST, instance=snapshot)
            if formset.is_valid():
                save_asset_snapshot_formset(formset, snapshot)
                return redirect("ledger:asset_snapshot_detail", pk=snapshot.pk)
    else:
        initial_entries = get_latest_snapshot_entry_initial()
        InitialAssetBalanceEntryFormSet = make_asset_balance_entry_formset(extra=len(initial_entries))
        form = AssetBalanceSnapshotForm(instance=snapshot, initial={"snapshot_date": timezone.localdate()})
        formset = InitialAssetBalanceEntryFormSet(instance=snapshot, initial=initial_entries)
    return render(
        request,
        "ledger/asset_snapshot_form.html",
        {
            "form": form,
            "formset": formset,
            "title": "新增资产快照",
            "account_member_map": get_account_member_map(),
            "account_options": get_account_options(),
        },
    )


@login_required
def asset_snapshot_edit(request, pk):
    snapshot = get_object_or_404(AssetBalanceSnapshot, pk=pk)
    if request.method == "POST":
        form = AssetBalanceSnapshotForm(request.POST, instance=snapshot)
        formset = AssetBalanceEntryFormSet(request.POST, instance=snapshot)
        if form.is_valid() and formset.is_valid():
            snapshot = form.save()
            save_asset_snapshot_formset(formset, snapshot)
            return redirect("ledger:asset_snapshot_detail", pk=snapshot.pk)
    else:
        form = AssetBalanceSnapshotForm(instance=snapshot)
        formset = AssetBalanceEntryFormSet(instance=snapshot)
    return render(
        request,
        "ledger/asset_snapshot_form.html",
        {
            "form": form,
            "formset": formset,
            "title": "编辑资产快照",
            "account_member_map": get_account_member_map(),
            "account_options": get_account_options(),
        },
    )


@login_required
def bank_account_list(request):
    accounts = BankAccount.objects.select_related("family", "member", "account_type_ref", "account_region").order_by("member__display_name", "account_name")
    return render(request, "ledger/bank_account_list.html", {"accounts": accounts})


@login_required
def bank_account_create(request):
    return save_form(request, BankAccountForm, "form.html", "ledger:category_list", "新增账户")


@login_required
def bank_account_edit(request, pk):
    account = get_object_or_404(BankAccount, pk=pk)
    return save_form(request, BankAccountForm, "form.html", "ledger:category_list", "编辑账户", account)


@login_required
def category_list(request):
    accounts = BankAccount.objects.select_related("family", "member", "account_type_ref", "account_region").order_by("member__display_name", "account_name")
    income_categories = IncomeCategory.objects.select_related("family", "parent").order_by("family__name", "name")
    expense_categories = ExpenseCategory.objects.select_related("family", "parent").order_by("family__name", "name")
    return render(
        request,
        "ledger/category_list.html",
        {"accounts": accounts, "income_categories": income_categories, "expense_categories": expense_categories},
    )


@login_required
def income_category_create(request):
    return save_form(request, IncomeCategoryForm, "form.html", "ledger:category_list", "新增收入分类")


@login_required
def income_category_edit(request, pk):
    category = get_object_or_404(IncomeCategory, pk=pk)
    return save_form(request, IncomeCategoryForm, "form.html", "ledger:category_list", "编辑收入分类", category)


@login_required
def expense_category_create(request):
    return save_form(request, ExpenseCategoryForm, "form.html", "ledger:category_list", "新增支出分类")


@login_required
def expense_category_edit(request, pk):
    category = get_object_or_404(ExpenseCategory, pk=pk)
    return save_form(request, ExpenseCategoryForm, "form.html", "ledger:category_list", "编辑支出分类", category)


@login_required
def income_list(request):
    records = IncomeRecord.objects.select_related("member", "category", "category__parent").order_by("-period_start", "-income_date", "-created_at")[:100]
    return render(request, "ledger/income_list.html", {"records": records})


@login_required
def income_create(request):
    return save_form(request, IncomeRecordForm, "form.html", "ledger:expense_list", "新增收入记录")


@login_required
def income_edit(request, pk):
    record = get_object_or_404(IncomeRecord, pk=pk)
    return save_form(request, IncomeRecordForm, "form.html", "ledger:expense_list", "编辑收入记录", record)


@login_required
def income_delete(request, pk):
    record = get_object_or_404(IncomeRecord, pk=pk)
    next_url = request.POST.get("next") if request.method == "POST" else None
    if request.method == "POST":
        record.delete()
    if next_url and next_url.startswith("/"):
        return redirect(next_url)
    return redirect("ledger:expense_list")


@login_required
def cashflow_summary(request):
    members, sections = build_cashflow_monthly_rows()
    return render(
        request,
        "ledger/cashflow_summary.html",
        {
            "members": members,
            "sections": sections,
        },
    )


@login_required
def expense_list(request):
    members, rows = build_annual_cashflow_rows()
    return render(request, "ledger/expense_year_list.html", {"members": members, "rows": rows})


@login_required
def expense_year_detail(request, year):
    report = build_year_cashflow_detail(year)
    return render(request, "ledger/expense_list.html", {"year": year, **report})


@login_required
def expense_create(request):
    return save_form(request, ExpenseRecordForm, "form.html", "ledger:expense_list", "新增支出记录")


@login_required
def expense_edit(request, pk):
    record = get_object_or_404(ExpenseRecord, pk=pk)
    return save_form(request, ExpenseRecordForm, "form.html", "ledger:expense_list", "编辑支出记录", record)


@login_required
def expense_delete(request, pk):
    record = get_object_or_404(ExpenseRecord, pk=pk)
    next_url = request.POST.get("next") if request.method == "POST" else None
    if request.method == "POST":
        record.delete()
    if next_url and next_url.startswith("/"):
        return redirect(next_url)
    return redirect("ledger:expense_list")
