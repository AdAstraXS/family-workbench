from django.contrib.auth.decorators import login_required
from django.db.models import Sum
from django.shortcuts import get_object_or_404, redirect, render

from .forms import (
    InvestmentAccountForm,
    InvestmentPositionForm,
    InvestmentTransactionForm,
    SecurityForm,
)
from .models import InvestmentAccount, InvestmentPosition, InvestmentTransaction, Security


def save_form(request, form_class, template_name, success_url_name, title, instance=None):
    if request.method == "POST":
        form = form_class(request.POST, instance=instance)
        if form.is_valid():
            form.save()
            return redirect(success_url_name)
    else:
        form = form_class(instance=instance)
    return render(request, template_name, {"form": form, "title": title})


@login_required
def overview(request):
    accounts = InvestmentAccount.objects.select_related("family", "member").filter(is_active=True)
    positions = InvestmentPosition.objects.select_related("account", "security").order_by("-position_date")[:20]
    total_cash = accounts.aggregate(total=Sum("cash_balance"))["total"] or 0
    total_market_value = InvestmentPosition.objects.aggregate(total=Sum("market_value"))["total"] or 0
    return render(
        request,
        "portfolio/overview.html",
        {
            "accounts": accounts[:10],
            "positions": positions,
            "total_cash": total_cash,
            "total_market_value": total_market_value,
        },
    )


@login_required
def account_list(request):
    accounts = InvestmentAccount.objects.select_related("family", "member").order_by("member__display_name", "broker_name")
    return render(request, "portfolio/account_list.html", {"accounts": accounts})


@login_required
def account_create(request):
    return save_form(request, InvestmentAccountForm, "form.html", "portfolio:account_list", "新增投资账户")


@login_required
def account_edit(request, pk):
    account = get_object_or_404(InvestmentAccount, pk=pk)
    return save_form(request, InvestmentAccountForm, "form.html", "portfolio:account_list", "编辑投资账户", account)


@login_required
def security_list(request):
    securities = Security.objects.order_by("market", "symbol")
    return render(request, "portfolio/security_list.html", {"securities": securities})


@login_required
def security_create(request):
    return save_form(request, SecurityForm, "form.html", "portfolio:security_list", "新增证券标的")


@login_required
def security_edit(request, pk):
    security = get_object_or_404(Security, pk=pk)
    return save_form(request, SecurityForm, "form.html", "portfolio:security_list", "编辑证券标的", security)


@login_required
def position_list(request):
    positions = InvestmentPosition.objects.select_related("account", "security").order_by("-position_date", "-updated_at")[:100]
    return render(request, "portfolio/position_list.html", {"positions": positions})


@login_required
def position_create(request):
    return save_form(request, InvestmentPositionForm, "form.html", "portfolio:position_list", "新增投资持仓")


@login_required
def position_edit(request, pk):
    position = get_object_or_404(InvestmentPosition, pk=pk)
    return save_form(request, InvestmentPositionForm, "form.html", "portfolio:position_list", "编辑投资持仓", position)


@login_required
def transaction_list(request):
    transactions = InvestmentTransaction.objects.select_related("account", "security").order_by("-trade_date", "-created_at")[:100]
    return render(request, "portfolio/transaction_list.html", {"transactions": transactions})


@login_required
def transaction_create(request):
    return save_form(request, InvestmentTransactionForm, "form.html", "portfolio:transaction_list", "新增交易记录")


@login_required
def transaction_edit(request, pk):
    transaction = get_object_or_404(InvestmentTransaction, pk=pk)
    return save_form(request, InvestmentTransactionForm, "form.html", "portfolio:transaction_list", "编辑交易记录", transaction)
