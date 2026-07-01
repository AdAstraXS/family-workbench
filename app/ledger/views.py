from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db.models import Q, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from datetime import date
from decimal import Decimal
import json
from urllib.parse import quote

from .forms import (
    AnnualBudgetForm,
    AnnualBudgetLineFormSet,
    AssetBalanceEntryFormSet,
    AssetBalanceSnapshotForm,
    BankAccountForm,
    ExpenseImportForm,
    ExpenseRecordForm,
    IncomeRecordForm,
    make_annual_budget_line_formset,
    make_asset_balance_entry_formset,
)
from .expense_import import ExpenseWorkbookError, import_expense_workbook
from .expense_export import build_expense_workbook
from .asset_snapshot_export import build_asset_snapshot_workbook
from .models import AnnualBudget, AnnualBudgetLine, AssetBalanceSnapshot, BankAccount, ExpenseCategory, ExpenseImportBatch, ExpenseRecord, IncomeCategory, IncomeRecord
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


def build_cashflow_monthly_rows(target_year=None):
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
        if target_year and year != target_year:
            continue
        ensure_month(year, month)["income"][record.member_id] += record.amount or Decimal("0")

    for record in expense_records:
        if record.member_id not in member_ids:
            continue
        year, month = get_record_month(record, "expense_date")
        if target_year and year != target_year:
            continue
        ensure_month(year, month)["expense"][record.member_id] += record.amount or Decimal("0")

    today = timezone.localdate()
    if not month_map and not target_year:
        ensure_month(today.year, today.month)

    rows = []
    year_totals = {}
    for key in sorted(month_map.keys()):
        row = month_map[key]
        row["income_cells"] = [row["income"][member.id] for member in members]
        row["expense_cells"] = [row["expense"][member.id] for member in members]
        row["net_cells"] = [row["income"][member.id] - row["expense"][member.id] for member in members]
        row["income_total"] = sum(row["income_cells"], Decimal("0"))
        row["expense_total"] = sum(row["expense_cells"], Decimal("0"))
        row["net_total"] = sum(row["net_cells"], Decimal("0"))
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
        year_total["income_total"] = sum(year_total["income_cells"], Decimal("0"))
        year_total["expense_total"] = sum(year_total["expense_cells"], Decimal("0"))
        year_total["net_total"] = sum(year_total["net_cells"], Decimal("0"))

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
    expense_records = ExpenseRecord.objects.select_related(
        "family",
        "member",
        "bank_account",
        "category",
        "category__parent",
        "category__parent__parent",
    )
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


def build_cashflow_trend_data(annual_rows, selected_year):
    def in_ten_thousands(amount):
        return float(
            ((amount or Decimal("0")) / Decimal("10000")).quantize(Decimal("0.01"))
        )

    if selected_year == "all":
        yearly_totals = {}
        for row in annual_rows:
            totals = yearly_totals.setdefault(
                row["year"],
                {"income": Decimal("0"), "expense": Decimal("0")},
            )
            totals["income"] += row["income_total"]
            totals["expense"] += row["expense_total"]
        labels = [str(year) for year in sorted(yearly_totals)]
        income = [in_ten_thousands(yearly_totals[int(label)]["income"]) for label in labels]
        expense = [in_ten_thousands(yearly_totals[int(label)]["expense"]) for label in labels]
        mode = "yearly"
    else:
        _, sections = build_cashflow_monthly_rows(selected_year)
        section = next((item for item in sections if item["year"] == selected_year), None)
        month_rows = {
            row["month"]: row
            for row in (section["rows"] if section else [])
        }
        labels = [f"{month}月" for month in range(1, 13)]
        income = [
            in_ten_thousands(month_rows.get(month, {}).get("income_total", Decimal("0")))
            for month in range(1, 13)
        ]
        expense = [
            in_ten_thousands(month_rows.get(month, {}).get("expense_total", Decimal("0")))
            for month in range(1, 13)
        ]
        mode = "monthly"
    return {
        "mode": mode,
        "unit": "万元",
        "labels": labels,
        "income": income,
        "expense": expense,
        "net": [
            round(income_amount - expense_amount, 2)
            for income_amount, expense_amount in zip(income, expense)
        ],
    }


def build_expense_category_pie_data(selected_year, selected_month=None, unit="万元"):
    _, _, expense_records = get_default_family_records()
    exact_category_totals = {}
    totals = {
        "primary": {},
        "secondary": {},
        "tertiary": {},
    }

    def add_total(level, category_id, name, amount, **relations):
        item = totals[level].setdefault(
            category_id,
            {
                "id": category_id,
                "name": name,
                "amount": Decimal("0"),
                **relations,
            },
        )
        item["amount"] += amount

    for record in expense_records:
        if selected_year != "all" and get_record_year(record, "expense_date") != selected_year:
            continue
        if selected_month and get_record_month(record, "expense_date") != (
            selected_year,
            selected_month,
        ):
            continue
        category_key = record.category_id or "uncategorized"
        exact_bucket = exact_category_totals.setdefault(
            category_key,
            {
                "category": record.category,
                "amount": Decimal("0"),
            },
        )
        exact_bucket["amount"] += record.amount or Decimal("0")

    for exact_bucket in exact_category_totals.values():
        amount = exact_bucket["amount"]
        if amount <= 0:
            continue
        category = exact_bucket["category"]
        if not category:
            add_total("primary", "uncategorized", "未分类", amount)
            add_total(
                "secondary",
                "uncategorized-secondary",
                "未细分至二级",
                amount,
                parent_id="uncategorized",
                parent_name="未分类",
            )
            add_total(
                "tertiary",
                "uncategorized-tertiary",
                "未细分至三级",
                amount,
                parent_id="uncategorized-secondary",
                parent_name="未细分至二级",
                primary_id="uncategorized",
                primary_name="未分类",
            )
            continue
        path = []
        visited = set()
        while category and category.pk not in visited:
            path.append(category)
            visited.add(category.pk)
            category = category.parent
        path.reverse()
        primary = path[0]
        add_total("primary", primary.pk, primary.name, amount)
        if len(path) >= 2:
            secondary = path[1]
            add_total(
                "secondary",
                secondary.pk,
                secondary.name,
                amount,
                parent_id=primary.pk,
                parent_name=primary.name,
            )
        else:
            secondary_id = f"primary-{primary.pk}-unallocated"
            add_total(
                "secondary",
                secondary_id,
                "未细分至二级",
                amount,
                parent_id=primary.pk,
                parent_name=primary.name,
            )
            add_total(
                "tertiary",
                f"{secondary_id}-tertiary",
                "未细分至二级",
                amount,
                parent_id=secondary_id,
                parent_name="未细分至二级",
                primary_id=primary.pk,
                primary_name=primary.name,
            )
        if len(path) >= 3:
            tertiary = path[2]
            add_total(
                "tertiary",
                tertiary.pk,
                tertiary.name,
                amount,
                parent_id=secondary.pk,
                parent_name=secondary.name,
                primary_id=primary.pk,
                primary_name=primary.name,
            )
        elif len(path) == 2:
            add_total(
                "tertiary",
                f"secondary-{secondary.pk}-unallocated",
                "未细分至三级",
                amount,
                parent_id=secondary.pk,
                parent_name=secondary.name,
                primary_id=primary.pk,
                primary_name=primary.name,
            )

    def serialize(level):
        items = []
        divisor = Decimal("1") if unit == "元" else Decimal("10000")
        for item in totals[level].values():
            if item["amount"] <= 0:
                continue
            serialized = {
                key: value
                for key, value in item.items()
                if key != "amount"
            }
            serialized["value"] = float(item["amount"] / divisor)
            items.append(serialized)
        return sorted(items, key=lambda item: (-item["value"], item["name"]))

    return {
        "unit": unit,
        "primary": serialize("primary"),
        "secondary": serialize("secondary"),
        "tertiary": serialize("tertiary"),
    }


def parse_filter_id(value):
    value = str(value or "").strip()
    return int(value) if value.isdigit() else None


def get_expense_filters(request):
    return {
        "member_id": parse_filter_id(request.GET.get("member")),
        "bank_account_id": parse_filter_id(request.GET.get("bank_account")),
        "primary_category_id": parse_filter_id(request.GET.get("primary_category")),
        "secondary_category_id": parse_filter_id(request.GET.get("secondary_category")),
        "tertiary_category_id": parse_filter_id(request.GET.get("tertiary_category")),
    }


def build_expense_filter_options(family):
    if not family:
        return {
            "expense_filter_accounts": [],
            "expense_primary_categories": [],
            "expense_secondary_categories": [],
            "expense_tertiary_categories": [],
        }
    categories = ExpenseCategory.objects.filter(family=family, is_active=True)
    return {
        "expense_filter_accounts": list(
            BankAccount.objects.filter(
                family=family,
                is_active=True,
                expense_records__isnull=False,
            )
            .select_related("member", "account_type_ref")
            .distinct()
            .order_by("member__display_name", "account_name")
        ),
        "expense_primary_categories": list(
            categories.filter(parent__isnull=True).order_by("name")
        ),
        "expense_secondary_categories": list(
            categories.filter(parent__isnull=False, parent__parent__isnull=True)
            .select_related("parent")
            .order_by("parent__name", "name")
        ),
        "expense_tertiary_categories": list(
            categories.filter(
                parent__isnull=False,
                parent__parent__isnull=False,
                parent__parent__parent__isnull=True,
            )
            .select_related("parent", "parent__parent")
            .order_by("parent__parent__name", "parent__name", "name")
        ),
    }


def build_year_cashflow_detail(year, month=None, expense_filters=None):
    default_family, income_records, expense_records = get_default_family_records()
    members = list(FamilyMember.objects.filter(family=default_family, is_active=True).order_by("display_name")) if default_family else []
    member_ids = [member.id for member in members]
    expense_filters = expense_filters or {}
    income_records = (
        income_records.filter(Q(period_start__year=year) | Q(period_start__isnull=True, income_date__year=year))
        .order_by("period_start", "income_date", "member__display_name", "created_at")
    )
    expense_records = (
        expense_records.filter(Q(period_start__year=year) | Q(period_start__isnull=True, expense_date__year=year))
        .order_by("period_start", "expense_date", "member__display_name", "created_at")
    )
    if expense_filters.get("member_id"):
        expense_records = expense_records.filter(member_id=expense_filters["member_id"])
    if expense_filters.get("bank_account_id"):
        expense_records = expense_records.filter(bank_account_id=expense_filters["bank_account_id"])
    if expense_filters.get("primary_category_id"):
        primary_id = expense_filters["primary_category_id"]
        expense_records = expense_records.filter(
            Q(category_id=primary_id)
            | Q(category__parent_id=primary_id)
            | Q(category__parent__parent_id=primary_id)
        )
    if expense_filters.get("secondary_category_id"):
        secondary_id = expense_filters["secondary_category_id"]
        expense_records = expense_records.filter(
            Q(category_id=secondary_id) | Q(category__parent_id=secondary_id)
        )
    if expense_filters.get("tertiary_category_id"):
        expense_records = expense_records.filter(
            category_id=expense_filters["tertiary_category_id"]
        )
    if month:
        income_records = [record for record in income_records if get_record_month(record, "income_date") == (year, month)]
        expense_records = [record for record in expense_records if get_record_month(record, "expense_date") == (year, month)]
    else:
        income_records = list(income_records)
        expense_records = list(expense_records)

    def make_row(record, kind):
        fallback_field = "income_date" if kind == "income" else "expense_date"
        category_names = record.category.path_names if kind == "expense" and record.category else []
        return {
            "kind": kind,
            "record": record,
            "period_label": (
                get_record_year_month_label(record, fallback_field)
                if kind == "income"
                else record.expense_date.strftime("%Y-%m-%d")
            ),
            "sort_key": record.period_start or getattr(record, fallback_field),
            "member": record.member,
            "category": record.category,
            "primary_category": category_names[0] if category_names else "",
            "secondary_category": category_names[1] if len(category_names) > 1 else "",
            "tertiary_category": category_names[2] if len(category_names) > 2 else "",
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

    report = {
        "family": default_family,
        "year": year,
        "month": month,
        "report_title": f"{year}年{month}月收支记录" if month else f"{year}年收支记录",
        "return_label": "返回月度收支" if month else "返回月度收支",
        "members": members,
        "income_rows": income_rows,
        "expense_rows": expense_rows,
        "income_member_totals": [{"member": member, "amount": income_member_totals[member.id]} for member in members],
        "expense_member_totals": [{"member": member, "amount": expense_member_totals[member.id]} for member in members],
        "income_family_total": sum(income_member_totals.values(), Decimal("0")),
        "expense_family_total": sum(expense_member_totals.values(), Decimal("0")),
        "expense_filters": expense_filters,
        "expense_filter_active": any(expense_filters.values()),
    }
    report.update(build_expense_filter_options(default_family))
    return report


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

LEGACY_EXPENSE_BUDGET_PATHS = {
    "经营性-餐饮": ("经常性-餐饮",),
    "经营性-交通": ("经常性-交通",),
    "经营性-通信及订阅费": (
        "经常性-生活服务-话费",
        "经常性-知识付费-会员付费",
    ),
    "经营性-日用百货": ("经常性-生活日用",),
    "经营性-母婴花费": ("经常性-养娃",),
    "固定资产-电器、数码产品": ("固定资产-家居家电",),
    "经营性-美容美发": (
        "经常性-穿搭美容-护肤",
        "经常性-穿搭美容-理发",
    ),
    "经营性-物业车位": (
        "经常性-生活服务-物业及租金",
        "经常性-生活服务-停车费",
    ),
    "经营性-水电燃气话费": (
        "经常性-生活服务-水电费",
        "经常性-生活服务-燃气费",
        "经常性-生活服务-话费",
    ),
    "经营性-加油费": ("经常性-爱车-加油",),
    "经营性-车辆保养": (
        "经常性-爱车-车检",
        "经常性-生活服务-洗车",
        "经常性-生活服务-维修费",
    ),
    "经营性-保险": ("经常性-金融保险",),
    "经营性-医疗保健": ("经常性-医疗保健",),
    "经营性-知识付费、买书、会员卡": ("经常性-知识付费",),
    "经营性-模型、游戏": ("经常性-休闲玩乐",),
    "经营性-旅游/交通出行": (
        "经常性-交通",
        "经常性-酒店旅行",
    ),
    "经营性-服饰包包": (
        "经常性-穿搭美容-衣服",
        "经常性-穿搭美容-男装",
    ),
    "营业外-人情世故礼物等": ("经常性-人情社交",),
}


def budget_category_label(line):
    category = line.income_category if line.line_type == AnnualBudgetLine.LINE_TYPE_INCOME else line.expense_category
    if category:
        return str(category)
    return str((line.extra_data or {}).get("category_path", ""))


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
        return None
    visited = set()
    while category.parent and category.pk not in visited:
        visited.add(category.pk)
        category = category.parent
    return category


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
            "expense_category__parent__parent",
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


def budget_category_record_ids(category, categories, aliases=None, fallback_path=""):
    category_path = str(category) if category else fallback_path
    if not category_path:
        return None
    target_paths = {category_path}
    if aliases:
        target_paths.update(aliases.get(category_path, ()))
    return {
        item.id
        for item in categories
        if any(
            str(item) == target_path or str(item).startswith(f"{target_path}-")
            for target_path in target_paths
        )
    }


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
    income_categories = list(
        IncomeCategory.objects.filter(family=budget.family).select_related("parent")
    )
    expense_categories = list(
        ExpenseCategory.objects.filter(family=budget.family).select_related(
            "parent",
            "parent__parent",
        )
    )

    total_income_budget = Decimal("0")
    total_expense_budget = Decimal("0")
    total_income_actual = Decimal("0")
    total_expense_actual = Decimal("0")
    line_rows = []

    for line in lines:
        actual = Decimal("0")
        if line.line_type == AnnualBudgetLine.LINE_TYPE_INCOME:
            total_income_budget += line.annual_amount or Decimal("0")
            category_ids = budget_category_record_ids(
                line.income_category,
                income_categories,
                fallback_path=budget_category_label(line),
            )
            for record in income_records:
                if category_ids is not None and record.category_id not in category_ids:
                    continue
                actual += record.amount or Decimal("0")
        else:
            total_expense_budget += line.annual_amount or Decimal("0")
            category_ids = budget_category_record_ids(
                line.expense_category,
                expense_categories,
                LEGACY_EXPENSE_BUDGET_PATHS,
                fallback_path=budget_category_label(line),
            )
            for record in expense_records:
                if category_ids is not None and record.category_id not in category_ids:
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
                "category_label": budget_category_label(line),
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
    active_expense_roots = list(
        ExpenseCategory.objects.filter(
            family=budget.family,
            parent__isnull=True,
            is_active=True,
        ).order_by("name")
    )
    expense_summary_rows = []
    for root in active_expense_roots:
        group_rows = [
            row
            for row in expense_line_rows
            if expense_budget_group(row) == root
        ]
        expense_summary_rows.append(
            build_budget_total_row(
                f"{root.name}汇总",
                group_rows,
                AnnualBudgetLine.LINE_TYPE_EXPENSE,
            )
        )
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
        category = (
            line.income_category
            if line.line_type == AnnualBudgetLine.LINE_TYPE_INCOME
            else line.expense_category
        )
        line.extra_data = {
            **(line.extra_data or {}),
            "category_path": str(category) if category else "",
        }
        line.save()


def save_asset_snapshot_formset(formset, snapshot):
    saved_ids = set()
    order = 1
    ordered_forms = sorted(
        formset.forms,
        key=lambda item: item.cleaned_data.get("display_order") or 10**9,
    )
    for form in ordered_forms:
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
        entry.original_amount = original_amount or Decimal("0")
        if entry.account:
            entry.account_name = entry.account.account_name
        entry.base_amount = calculate_base_amount(snapshot, entry.currency, entry.original_amount)
        entry.save()
        saved_ids.add(entry.pk)
        order += 1
    if snapshot.pk and saved_ids:
        snapshot.entries.exclude(pk__in=saved_ids).delete()


def allow_draft_blank_asset_amounts(formset):
    for form in formset.forms:
        form.fields["original_amount"].required = False


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
    latest_snapshot = (
        AssetBalanceSnapshot.objects.filter(is_draft=False)
        .order_by("-snapshot_date", "-created_at")
        .first()
    )
    if not latest_snapshot:
        return [{}]
    return [
        {
            "member": entry.member_id,
            "account": entry.account_id,
            "asset_category": entry.asset_category_id,
            "currency": entry.currency,
            "original_amount": None,
            "display_order": entry.display_order,
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
    previous_snapshot = (
        AssetBalanceSnapshot.objects.filter(
            family=snapshot.family,
            is_draft=False,
            snapshot_date__lt=snapshot.snapshot_date,
        )
        .order_by("-snapshot_date", "-created_at")
        .first()
    )
    previous_rates = {
        "USD": previous_snapshot.usd_to_base if previous_snapshot else snapshot.usd_to_base,
        "HKD": previous_snapshot.hkd_to_base if previous_snapshot else snapshot.hkd_to_base,
    }
    current_rates = {
        "USD": snapshot.usd_to_base,
        "HKD": snapshot.hkd_to_base,
    }
    member_exchange_gains = {member.id: Decimal("0") for member in members}
    for entry in entries:
        if entry.currency not in current_rates:
            continue
        member_exchange_gains[entry.member_id] += (
            (entry.original_amount or Decimal("0"))
            * (current_rates[entry.currency] - previous_rates[entry.currency])
        )
    exchange_gain_row = {
        "label": "汇兑损益金额",
        "cells": [member_exchange_gains[member.id] for member in members],
        "total": sum(member_exchange_gains.values(), Decimal("0")),
    }
    return (
        members,
        rows.values(),
        currency_total_rows,
        base_total_row,
        exchange_gain_row,
        grand_total,
    )


def get_month_start(day):
    return date(day.year, day.month, 1)


def get_next_month_start(day):
    if day.month == 12:
        return date(day.year + 1, 1, 1)
    return date(day.year, day.month + 1, 1)


def get_previous_month_start(day):
    if day.month == 1:
        return date(day.year - 1, 12, 1)
    return date(day.year, day.month - 1, 1)


def get_snapshot_member_totals(snapshot, members):
    totals = {member.id: Decimal("0") for member in members}
    if not snapshot:
        return totals
    for entry in snapshot.entries.all():
        if entry.member_id in totals:
            totals[entry.member_id] += entry.base_amount or Decimal("0")
    return totals


def get_cashflow_member_totals(family, members, start_date, end_date):
    member_ids = [member.id for member in members]
    income_totals = {member.id: Decimal("0") for member in members}
    expense_totals = {member.id: Decimal("0") for member in members}
    if not family:
        return {member.id: Decimal("0") for member in members}

    income_records = IncomeRecord.objects.filter(family=family).filter(
        Q(period_start__lte=end_date, period_end__gte=start_date)
        | Q(period_start__gte=start_date, period_start__lte=end_date, period_end__isnull=True)
        | Q(period_start__isnull=True, income_date__gte=start_date, income_date__lte=end_date)
    )
    expense_records = ExpenseRecord.objects.filter(family=family).filter(
        Q(period_start__lte=end_date, period_end__gte=start_date)
        | Q(period_start__gte=start_date, period_start__lte=end_date, period_end__isnull=True)
        | Q(period_start__isnull=True, expense_date__gte=start_date, expense_date__lte=end_date)
    )
    for record in income_records:
        if record.member_id in member_ids:
            income_totals[record.member_id] += record.amount or Decimal("0")
    for record in expense_records:
        if record.member_id in member_ids:
            expense_totals[record.member_id] += record.amount or Decimal("0")
    return {member.id: income_totals[member.id] - expense_totals[member.id] for member in members}


def build_investment_return_report():
    family = Family.objects.filter(name="我的家庭").first() or Family.objects.first()
    members = list(FamilyMember.objects.filter(family=family, is_active=True).order_by("display_name")) if family else []
    latest_snapshot = (
        AssetBalanceSnapshot.objects.filter(family=family, is_draft=False)
        .prefetch_related("entries")
        .order_by("-snapshot_date", "-created_at")
        .first()
        if family
        else None
    )
    if not latest_snapshot:
        return {
            "family": family,
            "members": members,
            "latest_snapshot": None,
            "previous_year_snapshot": None,
            "previous_month_snapshot": None,
            "rows": [],
        }

    latest_date = latest_snapshot.snapshot_date
    year_start = date(latest_date.year, 1, 1)
    month_start = get_month_start(latest_date)
    previous_year_end = date(latest_date.year - 1, 12, 31)
    previous_month_start = get_previous_month_start(latest_date)

    previous_year_snapshot = (
        AssetBalanceSnapshot.objects.filter(
            family=family,
            is_draft=False,
            snapshot_date__lte=previous_year_end,
        )
        .prefetch_related("entries")
        .order_by("-snapshot_date", "-created_at")
        .first()
    )
    previous_month_snapshot = (
        AssetBalanceSnapshot.objects.filter(
            family=family,
            is_draft=False,
            snapshot_date__gte=year_start,
            snapshot_date__lt=month_start,
        )
        .prefetch_related("entries")
        .order_by("-snapshot_date", "-created_at")
        .first()
    )

    previous_year_totals = get_snapshot_member_totals(previous_year_snapshot, members)
    previous_month_totals = get_snapshot_member_totals(previous_month_snapshot, members)
    current_totals = get_snapshot_member_totals(latest_snapshot, members)
    year_net_totals = get_cashflow_member_totals(family, members, year_start, latest_date)
    month_net_totals = get_cashflow_member_totals(family, members, month_start, latest_date)

    rows = []
    total_row = {
        "label": "家庭合计",
        "previous_year_balance": Decimal("0"),
        "previous_month_balance": Decimal("0"),
        "current_balance": Decimal("0"),
        "increase_from_year_end": Decimal("0"),
        "increase_from_previous_month": Decimal("0"),
        "year_net_cashflow": Decimal("0"),
        "month_net_cashflow": Decimal("0"),
        "year_investment_return": Decimal("0"),
        "month_investment_return": Decimal("0"),
        "is_total": True,
    }
    for member in members:
        previous_year_balance = previous_year_totals[member.id]
        previous_month_balance = previous_month_totals[member.id]
        current_balance = current_totals[member.id]
        year_net_cashflow = year_net_totals[member.id]
        month_net_cashflow = month_net_totals[member.id]
        row = {
            "label": member.display_name,
            "previous_year_balance": previous_year_balance,
            "previous_month_balance": previous_month_balance,
            "current_balance": current_balance,
            "increase_from_year_end": current_balance - previous_year_balance,
            "increase_from_previous_month": current_balance - previous_month_balance,
            "year_net_cashflow": year_net_cashflow,
            "month_net_cashflow": month_net_cashflow,
            "year_investment_return": current_balance - previous_year_balance - year_net_cashflow,
            "month_investment_return": current_balance - previous_month_balance - month_net_cashflow,
            "is_total": False,
        }
        rows.append(row)
        for key in total_row:
            if key not in {"label", "is_total"}:
                total_row[key] += row[key]
    rows.append(total_row)

    return {
        "family": family,
        "members": members,
        "latest_snapshot": latest_snapshot,
        "previous_year_snapshot": previous_year_snapshot,
        "previous_month_snapshot": previous_month_snapshot,
        "month_label": f"{latest_date.year}年{latest_date.month}月",
        "year_label": f"{latest_date.year}年",
        "rows": rows,
    }


@login_required
def overview(request):
    latest_snapshot = (
        AssetBalanceSnapshot.objects.filter(is_draft=False)
        .order_by("-snapshot_date", "-created_at")
        .first()
    )
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
def investment_return_report(request):
    report = build_investment_return_report()
    return render(request, "ledger/investment_return_report.html", report)


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
        savings_budget = summary["income_budget"] - summary["expense_budget"]
        savings_actual = summary["income_actual"] - summary["expense_actual"]
        savings_rate = (
            savings_actual / savings_budget * Decimal("100")
            if savings_budget
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
                "savings_budget": savings_budget,
                "savings_actual": savings_actual,
                "savings_rate": savings_rate,
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

    chronological_rows = [
        row for row in reversed(rows) if not row["snapshot"].is_draft
    ]
    monthly_points = {}
    yearly_points = {}
    for row in chronological_rows:
        snapshot_date = row["snapshot"].snapshot_date
        point = {
            "member_values": row["member_totals"],
            "family_value": row["total"],
        }
        monthly_points[f"{snapshot_date.year}-{snapshot_date.month:02d}"] = point
        yearly_points[str(snapshot_date.year)] = point

    def build_trend_period(points):
        labels = list(points)
        series = []
        for index, member in enumerate(members):
            series.append(
                {
                    "name": member.display_name,
                    "kind": "member",
                    "values": [
                        float(
                            (
                                points[label]["member_values"][index]
                                / Decimal("10000")
                            ).quantize(Decimal("0.01"))
                        )
                        for label in labels
                    ],
                }
            )
        series.append(
            {
                "name": "家庭合计",
                "kind": "family",
                "values": [
                    float(
                        (
                            points[label]["family_value"] / Decimal("10000")
                        ).quantize(Decimal("0.01"))
                    )
                    for label in labels
                ],
            }
        )
        return {"labels": labels, "series": series}

    trend_data = {
        "unit": "万元",
        "monthly": build_trend_period(monthly_points),
        "yearly": build_trend_period(yearly_points),
    }
    return render(
        request,
        "ledger/asset_snapshot_list.html",
        {"rows": rows, "members": members, "trend_data": trend_data},
    )


@login_required
def asset_snapshot_export(request):
    snapshots = list(
        AssetBalanceSnapshot.objects.select_related("family")
        .prefetch_related(
            "entries__member",
            "entries__account__account_region",
            "entries__account__account_type_ref",
            "entries__asset_category",
        )
        .order_by("-snapshot_date", "-created_at")
    )
    output = build_asset_snapshot_workbook(snapshots, build_asset_snapshot_matrix)
    filename = f"家庭资产快照_{timezone.localdate():%Y%m%d}.xlsx"
    response = HttpResponse(
        output.getvalue(),
        content_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
    )
    response["Content-Disposition"] = (
        f"attachment; filename*=UTF-8''{quote(filename)}"
    )
    return response


@login_required
def asset_snapshot_detail(request, pk):
    snapshot = get_object_or_404(AssetBalanceSnapshot.objects.select_related("family"), pk=pk)
    (
        members,
        rows,
        currency_totals,
        base_total_row,
        exchange_gain_row,
        grand_total,
    ) = build_asset_snapshot_matrix(snapshot)
    return render(
        request,
        "ledger/asset_snapshot_detail.html",
        {
            "snapshot": snapshot,
            "members": members,
            "rows": rows,
            "currency_totals": currency_totals,
            "base_total_row": base_total_row,
            "exchange_gain_row": exchange_gain_row,
            "grand_total": grand_total,
        },
    )


@login_required
def asset_snapshot_create(request):
    snapshot = AssetBalanceSnapshot()
    if request.method == "POST":
        save_as_draft = request.POST.get("save_action") == "draft"
        form = AssetBalanceSnapshotForm(request.POST, instance=snapshot)
        formset = AssetBalanceEntryFormSet(request.POST, instance=snapshot)
        if save_as_draft:
            allow_draft_blank_asset_amounts(formset)
        if form.is_valid() and formset.is_valid():
            snapshot = form.save(commit=False)
            snapshot.is_draft = save_as_draft
            snapshot.save()
            formset = AssetBalanceEntryFormSet(request.POST, instance=snapshot)
            if save_as_draft:
                allow_draft_blank_asset_amounts(formset)
            if formset.is_valid():
                save_asset_snapshot_formset(formset, snapshot)
                messages.success(
                    request,
                    "资产快照草稿已保存。"
                    if snapshot.is_draft
                    else "资产快照已保存。",
                )
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
        save_as_draft = request.POST.get("save_action") == "draft"
        form = AssetBalanceSnapshotForm(request.POST, instance=snapshot)
        formset = AssetBalanceEntryFormSet(request.POST, instance=snapshot)
        if save_as_draft:
            allow_draft_blank_asset_amounts(formset)
        if form.is_valid() and formset.is_valid():
            snapshot = form.save(commit=False)
            snapshot.is_draft = save_as_draft
            snapshot.save()
            save_asset_snapshot_formset(formset, snapshot)
            messages.success(
                request,
                "资产快照草稿已保存。"
                if snapshot.is_draft
                else "资产快照已保存。",
            )
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
    income_categories = IncomeCategory.objects.select_related("family", "parent", "parent__parent").order_by(
        "family__name", "parent__parent__name", "parent__name", "name"
    )
    expense_categories = ExpenseCategory.objects.select_related("family", "parent", "parent__parent").order_by(
        "family__name", "parent__parent__name", "parent__name", "name"
    )
    return render(
        request,
        "ledger/category_list.html",
        {"accounts": accounts, "income_categories": income_categories, "expense_categories": expense_categories},
    )


@login_required
def income_category_create(request):
    return redirect("admin:ledger_incomecategory_add")


@login_required
def income_category_edit(request, pk):
    get_object_or_404(IncomeCategory, pk=pk)
    return redirect("admin:ledger_incomecategory_change", object_id=pk)


@login_required
def expense_category_create(request):
    return redirect("admin:ledger_expensecategory_add")


@login_required
def expense_category_edit(request, pk):
    get_object_or_404(ExpenseCategory, pk=pk)
    return redirect("admin:ledger_expensecategory_change", object_id=pk)


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
    if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
        return redirect(next_url)
    return redirect("ledger:expense_list")


@login_required
def cashflow_summary(request, year=None):
    members, sections = build_cashflow_monthly_rows(year)
    requested_month = str(request.GET.get("category_month") or "").strip().lower()
    if requested_month.isdigit() and 1 <= int(requested_month) <= 12:
        selected_category_month = int(requested_month)
    else:
        selected_category_month = "all"
    return render(
        request,
        "ledger/cashflow_summary.html",
        {
            "members": members,
            "sections": sections,
            "year": year,
            "selected_category_month": selected_category_month,
            "expense_category_pie_data": (
                build_expense_category_pie_data(
                    year,
                    selected_category_month
                    if selected_category_month != "all"
                    else None,
                    unit="元",
                )
                if year
                else {"unit": "万元", "primary": [], "secondary": [], "tertiary": []}
            ),
        },
    )


@login_required
def expense_year_export(request, year):
    default_family, _, expense_records = get_default_family_records()
    records = list(
        expense_records.filter(
            Q(period_start__year=year)
            | Q(period_start__isnull=True, expense_date__year=year)
        )
        .select_related("import_batch")
        .order_by(
            "expense_date",
            "occurred_at",
            "member__display_name",
            "created_at",
        )
    )
    output = build_expense_workbook(records, year)
    family_name = default_family.name if default_family else "家庭"
    filename = f"{family_name}_{year}年支出明细.xlsx"
    response = HttpResponse(
        output.getvalue(),
        content_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
    )
    response["Content-Disposition"] = (
        f"attachment; filename*=UTF-8''{quote(filename)}"
    )
    return response


@login_required
def expense_list(request):
    members, rows = build_annual_cashflow_rows()
    available_years = sorted({row["year"] for row in rows}, reverse=True)
    requested_year = str(request.GET.get("trend_year") or "").strip().lower()
    if requested_year == "all":
        selected_year = "all"
    elif requested_year.isdigit() and int(requested_year) in available_years:
        selected_year = int(requested_year)
    else:
        selected_year = available_years[0] if available_years else timezone.localdate().year
    requested_category_year = str(request.GET.get("category_year") or "").strip().lower()
    if requested_category_year == "all":
        selected_category_year = "all"
    elif requested_category_year.isdigit() and int(requested_category_year) in available_years:
        selected_category_year = int(requested_category_year)
    else:
        selected_category_year = available_years[0] if available_years else timezone.localdate().year
    return render(
        request,
        "ledger/expense_year_list.html",
        {
            "members": members,
            "rows": rows,
            "available_years": available_years,
            "selected_trend_year": selected_year,
            "cashflow_trend_data": build_cashflow_trend_data(rows, selected_year),
            "selected_category_year": selected_category_year,
            "expense_category_pie_data": build_expense_category_pie_data(
                selected_category_year
            ),
        },
    )


@login_required
def expense_year_detail(request, year):
    report = build_year_cashflow_detail(year, expense_filters=get_expense_filters(request))
    return render(request, "ledger/expense_list.html", {"year": year, **report})


@login_required
def expense_month_detail(request, year, month):
    report = build_year_cashflow_detail(year, month, get_expense_filters(request))
    return render(request, "ledger/expense_list.html", {"year": year, "month": month, **report})


@login_required
def expense_create(request):
    return save_form(request, ExpenseRecordForm, "ledger/expense_form.html", "ledger:expense_list", "手动录入支出")


@login_required
def expense_edit(request, pk):
    record = get_object_or_404(ExpenseRecord, pk=pk)
    return save_form(request, ExpenseRecordForm, "ledger/expense_form.html", "ledger:expense_list", "编辑支出记录", record)


@login_required
def expense_import(request):
    if request.method == "POST":
        form = ExpenseImportForm(request.POST, request.FILES)
        if form.is_valid():
            try:
                result = import_expense_workbook(
                    family=form.cleaned_data["family"],
                    uploaded_file=form.cleaned_data["workbook"],
                    imported_by=request.user,
                )
            except ExpenseWorkbookError as exc:
                form.add_error("workbook", str(exc))
            else:
                batch = result.batch
                if result.duplicate_file:
                    messages.info(request, f"该文件已导入过，本次未重复写入。原批次共 {batch.imported_count} 笔。")
                else:
                    messages.success(
                        request,
                        f"导入完成：读取 {batch.row_count} 笔，新增 {batch.imported_count} 笔，"
                        f"跳过重复 {batch.skipped_count} 笔，净支出 {batch.total_amount:,.2f} 元。",
                    )
                return redirect("ledger:expense_import")
    else:
        form = ExpenseImportForm()
    batches = ExpenseImportBatch.objects.select_related("family", "imported_by").order_by("-created_at")[:20]
    return render(
        request,
        "ledger/expense_import.html",
        {
            "form": form,
            "batches": batches,
            "expected_headers": (
                "支出时间",
                "所属账户",
                "支出账户",
                "一级分类",
                "二级分类",
                "三级分类",
                "金额",
                "备注",
            ),
        },
    )


@login_required
def expense_delete(request, pk):
    record = get_object_or_404(ExpenseRecord, pk=pk)
    next_url = request.POST.get("next") if request.method == "POST" else None
    if request.method == "POST":
        record.delete()
    if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
        return redirect(next_url)
    return redirect("ledger:expense_list")
