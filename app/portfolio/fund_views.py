from datetime import timedelta
from decimal import Decimal
from urllib.parse import urlencode
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST
from family_core.navigation import return_url
from .fund_models import FundDcaPlan, FundReference, FundRefreshRun
from .fund_forms import FundDcaForm, FundBalanceForm
from .fund_services import request_scope, can_edit, save_plan
from .fund_jobs import enqueue

STATUS = {"queued": "等待更新", "running": "正在更新", "saved": "更新完成",
          "partial": "部分更新", "failed": "更新失败", "interrupted": "已中断"}


def list_url(member="all"):
    return reverse("portfolio:fund_dca_list") + "?" + urlencode({"member": member})


@login_required
def fund_dca_list(request):
    family, member, manage_all = request_scope(request)
    members = family.members.all()
    selected = request.GET.get("member", "all")
    plans = FundDcaPlan.objects.filter(family=family).select_related("member", "fund")
    label = "全家庭"
    if selected != "all":
        chosen = members.filter(pk=int(selected)).first() if selected.isdigit() else None
        if chosen is None:
            raise Http404("家庭成员不存在")
        plans = plans.filter(member=chosen)
        label = chosen.display_name
    plans = list(plans)
    totals = {"monthly": Decimal("0"), "annual": Decimal("0"), "balance": Decimal("0")}
    charts = []
    for plan in plans:
        plan.can_edit = can_edit(request.user, member, manage_all, plan)
        totals["monthly"] += plan.monthly_amount
        totals["annual"] += plan.annual_amount
        totals["balance"] += plan.balance if plan.balance is not None else Decimal("0")
        charts.append({"label": f"{plan.name} · {plan.member.display_name}", "fund": plan.fund.code,
                       "monthly": str(plan.monthly_amount), "annual": str(plan.annual_amount),
                       "balance": str(plan.balance) if plan.balance is not None else None})
    jobs = list(FundRefreshRun.objects.filter(family=family)[:5])
    for job in jobs:
        expired = job.status in ["queued", "running"] and job.expires_at <= timezone.now()
        job.status_label = "已超时，请重新更新" if expired else STATUS.get(job.status, job.status)
        job.is_active = not expired and job.status in ["queued", "running"]
    return render(request, "portfolio/fund_list.html", {
        "plans": plans, "members": members, "selected": selected, "scope_label": label,
        "totals": totals, "charts": charts, "unknown_balances": sum(p.balance is None for p in plans),
        "can_create": can_edit(request.user, member, manage_all), "can_refresh": can_edit(request.user, member, manage_all),
        "jobs": jobs, "active_job": any(j.is_active for j in jobs), "return_to": list_url(selected),
        "page_parent_url": reverse("portfolio:overview"), "workspace_title": "基金定投"})


@login_required
def fund_dca_edit(request, pk=None, balance_only=False):
    family, member, manage_all = request_scope(request)
    plan = get_object_or_404(FundDcaPlan.objects.select_related("fund", "member"), pk=pk, family=family) if pk else None
    if not can_edit(request.user, member, manage_all, plan):
        raise PermissionDenied("当前账户不能维护这条定投。")
    fallback = list_url()
    back = return_url(request, fallback)
    if balance_only:
        form = FundBalanceForm(request.POST or None, instance=plan,
            initial={"revision": plan.revision, "balance_date": plan.balance_date or timezone.localdate()})
    else:
        form = FundDcaForm(request.POST or None, instance=plan, family=family,
                          actor_member=member, can_manage_all=manage_all)
    if request.method == "POST" and form.is_valid():
        try:
            saved = save_plan(form, family=family, user=request.user, member=member,
                              manage_all=manage_all, balance_only=balance_only)
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            if not balance_only:
                enqueue(family, request.user, [saved.fund_id])
            messages.success(request, "余额已保存。" if balance_only else "定投已保存；基金资料更新结果可在列表查看。")
            return redirect(back)
    return render(request, "portfolio/fund_form.html", {"form": form, "plan": plan,
        "balance_only": balance_only, "return_to": back, "page_parent_url": back,
        "workspace_title": "更新定投余额" if balance_only else ("编辑定投" if plan else "新增定投")})


@login_required
@require_POST
def fund_dca_refresh(request):
    family, member, manage_all = request_scope(request)
    if not can_edit(request.user, member, manage_all):
        raise PermissionDenied("当前账户只有查看权限。")
    plans = FundDcaPlan.objects.filter(family=family)
    if not manage_all:
        plans = plans.filter(member=member)
    ids = list(plans.values_list("fund_id", flat=True).distinct())
    if ids:
        job = enqueue(family, request.user, ids)
        messages.info(request, "基金更新任务已排队；可在页面查看状态。" if job.status == "queued" else "已有基金更新任务正在执行。")
    else:
        messages.info(request, "请先新增自己的定投。")
    return redirect(return_url(request, list_url()))


@login_required
def fund_detail(request, code):
    family, member, manage_all = request_scope(request)
    fund = get_object_or_404(FundReference.objects.filter(
        Q(dca_plans__family=family) | Q(feeder_funds__dca_plans__family=family)).distinct(), code=code)
    selected = request.GET.get("member", "all")
    if selected != "all" and (not selected.isdigit() or not family.members.filter(pk=int(selected)).exists()):
        selected = "all"
    back = list_url(selected)
    period = request.GET.get("period", "1y")
    if period not in ["3m", "6m", "1y", "all"]:
        period = "1y"
    navs = fund.nav_records.all()
    if period != "all":
        navs = navs.filter(nav_date__gte=timezone.localdate() - timedelta(days={"3m": 90, "6m": 180, "1y": 365}[period]))
    points = [[row.nav_date.isoformat(), str(row.unit_nav)] for row in navs]
    disclosures = list(fund.disclosures.all()[:12])
    allocation = next((d for d in disclosures if d.allocation_fetched_at), None)
    holdings = next((d for d in disclosures if d.holdings_fetched_at), None)
    for report in [allocation]:
        if report:
            for item in report.allocations:
                item["bar"] = str(max(Decimal("0"), min(Decimal("100"), Decimal(item["percentage"]))))
    target_report = fund.target_fund.disclosures.filter(holdings_fetched_at__isnull=False).first() if fund.target_fund_id else None
    return render(request, "portfolio/fund_detail.html", {"fund": fund, "period": period,
        "points": points, "allocation": allocation, "holdings": holdings, "target_report": target_report,
        "return_to": back, "selected": selected, "page_parent_url": back, "workspace_title": "基金资料",
        "periods": [("3m", "近3个月"), ("6m", "近6个月"), ("1y", "近1年"), ("all", "成立以来")]})
