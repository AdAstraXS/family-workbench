from django.contrib.auth.decorators import login_required
from django.db.models import Q, Sum
from django.shortcuts import render
from django.utils import timezone

from ledger.models import AssetBalanceSnapshot, ExpenseRecord, IncomeRecord
from portfolio.models import (
    InvestmentAccount,
    InvestmentTransaction,
)
from portfolio.valuation import value_portfolio
from ledger.valuation import cashflow_amount, MissingCashflowRate
from family_core.household import get_household_family
from .presentation import homepage_details


@login_required
def home(request):
    today = timezone.localdate()
    month_start = today.replace(day=1)
    family = get_household_family()
    valuation = value_portfolio(InvestmentAccount.objects.filter(
        bank_account__family=family, bank_account__is_active=True,
        bank_account__supports_investment=True,
    ), "CNY", today)
    latest_snapshot = AssetBalanceSnapshot.objects.filter(family=family, is_draft=False).order_by("-snapshot_date", "-created_at").first()
    asset_snapshot_total = latest_snapshot.entries.aggregate(total=Sum("base_amount"))["total"] if latest_snapshot else 0
    asset_snapshot_total = asset_snapshot_total or 0
    income_records = IncomeRecord.objects.filter(family=family).filter(
        Q(period_start__lte=today, period_end__gte=month_start)
        | Q(period_start__isnull=True, income_date__year=today.year, income_date__month=today.month)
    )
    expense_records = ExpenseRecord.objects.filter(family=family).filter(
        Q(period_start__lte=today, period_end__gte=month_start)
        | Q(period_start__isnull=True, expense_date__year=today.year, expense_date__month=today.month)
    )
    cashflow_errors = []
    def total(records):
        try:
            return sum(cashflow_amount(record) for record in records)
        except MissingCashflowRate as exc:
            cashflow_errors.append(str(exc))
            return None
    month_income, month_expense = total(income_records), total(expense_records)
    recent_transactions = (
        InvestmentTransaction.objects.filter(
            account__bank_account__family=family,
            account__bank_account__is_active=True,
            account__bank_account__supports_investment=True,
        )
        .select_related("account__bank_account", "security")
        .order_by("-trade_date", "-created_at")[:5]
    )
    recent_expenses = ExpenseRecord.objects.filter(family=family).select_related("member", "category").order_by("-period_start", "-expense_date", "-created_at")[:5]
    return render(
        request,
        "dashboard/home.html",
        {
            **homepage_details(family, getattr(request, "family_member", None), latest_snapshot, today),
            "total_investment_asset": None if valuation["missing_rates"] or valuation["missing_prices"] else valuation["total_asset"],
            "valuation": valuation,
            "valuation_date": today,
            "cashflow_errors": cashflow_errors,
            "bank_total": asset_snapshot_total,
            "latest_snapshot": latest_snapshot,
            "month_income": month_income,
            "month_expense": month_expense,
            "month_net": month_income - month_expense if month_income is not None and month_expense is not None else None,
            "recent_transactions": recent_transactions,
            "recent_expenses": recent_expenses,
        },
    )
