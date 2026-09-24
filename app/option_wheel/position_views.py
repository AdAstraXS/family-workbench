"""Human review of real portfolio positions, without brokerage actions."""

from datetime import date, timedelta
from zoneinfo import ZoneInfo

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from portfolio.models import InvestmentPosition, InvestmentTransaction, OptionContract, Security, TradeStatusChoices, TradeTypeChoices

from .models import WheelPositionReview, WheelPositionScanJob
from .position_evidence import participating_accounts
from .position_scan import comparison_rows
from .position_scan_jobs import enqueue as enqueue_position_scan
from .position_summary import option_position_rows
from .put_quote_jobs import enqueue as enqueue_put_quotes
from .views import PARTICIPATING_ACCOUNTS, _request_family


@login_required
def position_detail(request, pk):
    family = _request_family(request)
    row = next((item for item in option_position_rows(family) if item["position"].pk == pk), None)
    if row is None:
        from django.http import Http404
        raise Http404("未找到当前家庭的期权持仓。")
    job = WheelPositionScanJob.objects.filter(family=family, position=row["position"]).first()
    position_changed = bool(job and job.status == "saved" and (
        job.result.get("position_quantity") != str(row["position"].quantity)
        or job.result.get("position_avg_cost") != str(row["position"].avg_cost)
        or job.result.get("position_date") != row["position"].position_date.isoformat()
    ))
    comparison = (
        comparison_rows(row, job.result, job.started_at or job.created_at)
        if job and job.status == "saved" and not position_changed else None
    )
    today = timezone.now().astimezone(ZoneInfo("America/New_York")).date()
    suggested = max(row["contract"].expiration_date + timedelta(days=7), today + timedelta(days=1))
    return render(request, "option_wheel/position_detail.html", {
        "item": row, "job": job, "comparison": comparison,
        "suggested_expiration": suggested, "position_changed": position_changed,
    })


@login_required
@require_POST
def scan_position(request, pk):
    family = _request_family(request)
    if not request.user.is_superuser:
        raise PermissionDenied("只有管理员可查询 Futu 持仓候选。")
    row = next((item for item in option_position_rows(family) if item["position"].pk == pk), None)
    if row is None:
        from django.http import Http404
        raise Http404("未找到当前家庭的期权持仓。")
    try:
        target = date.fromisoformat(request.POST.get("target_expiration", ""))
    except ValueError:
        return HttpResponseBadRequest("请选择有效的目标到期日。")
    today = timezone.now().astimezone(ZoneInfo("America/New_York")).date()
    if target <= today or target > today + timedelta(days=190):
        return HttpResponseBadRequest("目标到期日须在未来 190 天内。")
    job = enqueue_position_scan(family, request.user, row["position"], target)
    if job.position_id != row["position"].pk:
        messages.warning(request, "已有另一张持仓的行情任务正在运行；请等它结束后再提交。")
    return redirect("option_wheel:position_detail", pk=pk)


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


@login_required
@require_POST
def refresh_put_quotes(request):
    family = _request_family(request)
    if not request.user.is_superuser:
        raise PermissionDenied("只有管理员可查询 Futu 持仓报价。")
    enqueue_put_quotes(family, request.user)
    return redirect("option_wheel:holdings")
