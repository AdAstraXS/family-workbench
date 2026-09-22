"""Human review of real portfolio positions, without brokerage actions."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST

from portfolio.models import InvestmentPosition, InvestmentTransaction, OptionContract, Security, TradeStatusChoices, TradeTypeChoices

from .models import WheelPositionReview
from .position_evidence import participating_accounts
from .views import PARTICIPATING_ACCOUNTS, _request_family


@login_required
@require_POST
def record_put_review(request):
    family = _request_family(request)
    if not request.user.is_superuser:
        raise PermissionDenied("只有管理员可记录人工决策。")
    position = get_object_or_404(
        InvestmentPosition.objects.select_related("account__bank_account", "security__option_contract"),
        pk=request.POST.get("position_id"), account__bank_account__family=family,
        account__bank_account__account_name__in=PARTICIPATING_ACCOUNTS,
        security__asset_type=Security.TYPE_OPTION,
        security__option_contract__option_type=OptionContract.PUT, quantity__lt=0,
    )
    if position.account_id not in {account.pk for account in participating_accounts(family).values()}:
        raise PermissionDenied("账户映射尚未确认。")
    choice = request.POST.get("choice", "")
    if choice not in dict(WheelPositionReview.CHOICES):
        return HttpResponseBadRequest("请选择处理方案。")
    note = request.POST.get("note", "").strip()
    if len(note) > 2000:
        return HttpResponseBadRequest("说明不得超过 2000 字。")
    WheelPositionReview.objects.create(
        family=family, account=position.account, security=position.security,
        choice=choice, note=note, created_by=request.user,
        frozen_facts={
            "quantity": str(position.quantity), "avg_cost": str(position.avg_cost),
            "current_price": str(position.current_price),
            "current_price_as_of": position.current_price_as_of.isoformat() if position.current_price_as_of else None,
            "position_date": position.position_date.isoformat(),
        },
    )
    messages.success(request, "人工判断已记录；真实持仓和交易流水没有改变。")
    return redirect("option_wheel:holdings")


@login_required
@require_POST
def link_put_transaction(request, pk):
    family = _request_family(request)
    if not request.user.is_superuser:
        raise PermissionDenied("只有管理员可关联交易。")
    review = get_object_or_404(WheelPositionReview, pk=pk, family=family)
    if review.linked_transaction_id:
        return HttpResponseBadRequest("此记录已关联真实交易；请保留审计记录。")
    trade = get_object_or_404(
        InvestmentTransaction,
        pk=request.POST.get("transaction_id"), account=review.account,
        security=review.security, status__in=[TradeStatusChoices.COMPLETED, TradeStatusChoices.PARTIAL],
        trade_type=TradeTypeChoices.BUY, position_effect=InvestmentTransaction.EFFECT_CLOSE,
    )
    review.linked_transaction = trade
    review.save(update_fields=["linked_transaction", "updated_at"])
    messages.success(request, "已关联投资组合中的真实交易；没有创建或修改交易。")
    return redirect("option_wheel:holdings")
