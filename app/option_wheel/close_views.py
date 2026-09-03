"""Separate daily observation page. GET only reads; POST never invokes live analysis."""

from datetime import date
from uuid import UUID, uuid4

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.exceptions import PermissionDenied
from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from portfolio.futu_option_probe import ProbeLock
from .account_capacity import capacity_snapshot_stale_reasons
from .close_data import CloseDataError, SYMBOLS, fetch_close_report
from .models import WheelBrokerAccountSnapshot, WheelCloseReport, WheelPolicy, WheelPause
from .views import PARTICIPATING_ACCOUNTS, _request_family, _snapshot_is_ready

SALT = "wheel-close-request-v1"


def policies_for(family):
    return WheelPolicy.objects.filter(
        family=family, enabled=True, underlying__symbol__in=SYMBOLS,
        account__bank_account__family=family,
        account__bank_account__account_name__in=PARTICIPATING_ACCOUNTS,
    ).select_related("account__bank_account", "underlying")


def account_evidence(policies, now):
    accounts = []
    for policy in policies:
        snapshot = WheelBrokerAccountSnapshot.objects.filter(
            family=policy.family, account=policy.account,
        ).order_by("-source_as_of", "-pk").first()
        stale = capacity_snapshot_stale_reasons(snapshot) if snapshot else []
        ready = _snapshot_is_ready(
            snapshot, max_age_minutes=policy.account_snapshot_max_age_minutes, now=now, stale_reasons=stale,
        )
        accounts.append({
            "account_id": policy.account_id, "name": policy.account.account_name,
            "snapshot_id": snapshot.pk if snapshot else None,
            "source_as_of": snapshot.source_as_of.isoformat() if snapshot else None,
            "checked_at": now.isoformat(), "ready_at_collection": ready,
            "state": "采集时容量就绪（不是下单许可）" if ready else "容量缺失、过期或已变化，需重新确认",
            "reasons": stale,
            "cash": str(snapshot.settled_cash) if snapshot and snapshot.settled_cash is not None else None,
            "nav": str(snapshot.nav) if snapshot and snapshot.nav is not None else None,
            "currency": snapshot.currency if snapshot else None,
        })
    return accounts


@login_required
@require_GET
def index(request):
    family = _request_family(request)
    policies = policies_for(family)
    return render(request, "option_wheel/close_index.html", {
        "symbols": sorted({p.underlying.symbol for p in policies}),
        "request_token": signing.dumps({"family": family.pk, "key": str(uuid4())}, salt=SALT),
        "reports": WheelCloseReport.objects.filter(family=family)[:12],
    })


@login_required
@require_GET
def detail(request, pk):
    report = get_object_or_404(WheelCloseReport, pk=pk, family=_request_family(request))
    return render(request, "option_wheel/close_detail.html", {"report": report, "evidence": report.evidence})


def response(request, outcome, message):
    if request.headers.get("Accept") == "application/json":
        return JsonResponse({"kind": "option-wheel-analysis-v1", "outcome": outcome, "message": message})
    (messages.success if outcome == "saved" else messages.error)(request, message)
    return redirect("option_wheel:close_index")


@login_required
@require_POST
def refresh(request):
    family = _request_family(request)
    if not request.user.is_superuser:
        raise PermissionDenied("只有管理员可以采集并保存收盘观察证据。")
    if request.POST.get("confirm_read_only") != "yes":
        return HttpResponseBadRequest("请确认只保存观察证据，不下单。")
    symbols = request.POST.getlist("symbols")
    if len(symbols) != 1 or symbols[0] not in SYMBOLS:
        return HttpResponseBadRequest("每次请选择首版范围内的一只标的。")
    symbol = symbols[0]
    policies = list(policies_for(family).filter(underlying__symbol=symbol))
    if not policies:
        return HttpResponseBadRequest("本家庭尚未启用该标的策略。")
    try:
        token = signing.loads(request.POST.get("request_token", ""), salt=SALT, max_age=7200)
        if token["family"] != family.pk:
            raise ValueError("wrong family")
        key = UUID(token["key"])
    except (signing.BadSignature, KeyError, TypeError, ValueError):
        return HttpResponseBadRequest("提交凭证无效或过期，请重新打开页面。")

    def existing_result():
        existing = WheelCloseReport.objects.filter(family=family, request_key=key).first()
        if existing:
            return response(request, "saved", f"该请求已保存为收盘观察 #{existing.pk}（{existing.symbol}）；未重复抓取。请重新读取记录。")

    existing = existing_result()
    if existing is not None:
        return existing
    # Same host-wide lock as the live probe: do not compete for OpenD capacity.
    lock = ProbeLock()
    if not lock.acquire():
        return response(request, "not_saved", "已有行情查询正在运行，本次未保存，请稍后重新读取记录。")
    try:
        existing = existing_result()
        if existing is not None:
            return existing
        evidence = fetch_close_report(symbol)
        # Check account evidence AFTER the slow query, never extend a confirmation.
        checked_at = timezone.now()
        evidence["accounts"] = account_evidence(policies, checked_at)
        evidence["pause_active"] = any(
            pause.starts_at <= checked_at and (pause.ends_at is None or pause.ends_at > checked_at)
            and (pause.underlying_id is None or pause.underlying_id == policies[0].underlying_id)
            for pause in WheelPause.objects.filter(family=family)
        )
        report = WheelCloseReport.objects.create(
            family=family, symbol=symbol, target_date=date.fromisoformat(evidence["target_date"]),
            request_key=key, evidence=evidence,
        )
        return response(request, "saved", f"已保存收盘观察 #{report.pk}，数据日 {report.target_date}。仅作观察，财报/除息及开盘报价仍待核验。")
    except CloseDataError as exc:
        return response(request, "not_saved", str(exc))
    finally:
        lock.release()
