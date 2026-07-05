from django.contrib.auth.decorators import login_required
from django.db.models import Q, Sum
from django.shortcuts import render
from django.utils import timezone

from ledger.models import AssetBalanceSnapshot, ExpenseRecord, IncomeRecord
from portfolio.models import (
    InvestmentCashMovement,
    InvestmentPosition,
    InvestmentTransaction,
)


@login_required
def home(request):
    today = timezone.localdate()
    month_start = today.replace(day=1)
    total_cash = InvestmentCashMovement.objects.filter(
        account__is_active=True,
    ).aggregate(total=Sum("amount"))["total"] or 0
    total_market_value = InvestmentPosition.objects.aggregate(total=Sum("market_value"))["total"] or 0
    latest_snapshot = AssetBalanceSnapshot.objects.order_by("-snapshot_date", "-created_at").first()
    asset_snapshot_total = latest_snapshot.entries.aggregate(total=Sum("base_amount"))["total"] if latest_snapshot else 0
    asset_snapshot_total = asset_snapshot_total or 0
    month_income = IncomeRecord.objects.filter(
        Q(period_start__lte=today, period_end__gte=month_start)
        | Q(period_start__isnull=True, income_date__year=today.year, income_date__month=today.month)
    ).aggregate(total=Sum("amount"))["total"] or 0
    month_expense = ExpenseRecord.objects.filter(
        Q(period_start__lte=today, period_end__gte=month_start)
        | Q(period_start__isnull=True, expense_date__year=today.year, expense_date__month=today.month)
    ).aggregate(total=Sum("amount"))["total"] or 0
    recent_transactions = InvestmentTransaction.objects.select_related("account", "security").order_by("-trade_date", "-created_at")[:5]
    recent_expenses = ExpenseRecord.objects.select_related("member", "category").order_by("-period_start", "-expense_date", "-created_at")[:5]
    return render(
        request,
        "dashboard/home.html",
        {
            "total_investment_asset": total_cash + total_market_value,
            "bank_total": asset_snapshot_total,
            "latest_snapshot": latest_snapshot,
            "month_income": month_income,
            "month_expense": month_expense,
            "month_net": month_income - month_expense,
            "recent_transactions": recent_transactions,
            "recent_expenses": recent_expenses,
        },
    )
