"""Simple household-facing weekly option screener."""

import re
from datetime import date, time, timedelta
from decimal import Decimal, InvalidOperation
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.exceptions import PermissionDenied
from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from portfolio.models import InvestmentAccount, PortfolioSnapshot, Security
from portfolio.valuation import resolve_exchange_rate

from .models import WheelAnalysisJob, WheelWatchItem
from .views import PARTICIPATING_ACCOUNTS, _request_family, _select_participating_account


NY = ZoneInfo("America/New_York")
SYMBOL_PATTERN = re.compile(r"[A-Z][A-Z0-9.]{0,10}")


def account_summary(family):
    from .position_evidence import stock_evidence
    from .position_summary import option_position_rows

    stock_rows = stock_evidence(family)
    option_rows = option_position_rows(family, stocks=stock_rows)
    groups = {name: [] for name in PARTICIPATING_ACCOUNTS}
    accounts = InvestmentAccount.objects.filter(
        bank_account__family=family,
        bank_account__is_active=True,
        bank_account__account_name__in=PARTICIPATING_ACCOUNTS,
    ).select_related("bank_account", "bank_account__member")
    for account in accounts:
        groups[account.account_name].append(account)
    result = []
    for name, matches in groups.items():
        account, ambiguous = _select_participating_account(matches)
        row = {"name": name, "cash": None, "nav": None, "as_of": None, "note": "尚无投资组合快照", "stocks": [], "options": []}
        if ambiguous:
            row["note"] = "账户映射不唯一"
        elif account is not None:
            row["options"] = [item for item in option_rows if item["account_id"] == account.pk]
            row["stocks"] = sorted(
                (stock for (account_id, _), stock in stock_rows.items() if account_id == account.pk),
                key=lambda stock: stock["symbol"],
            )
            snapshot = PortfolioSnapshot.objects.filter(family=family, account=account).order_by("-snapshot_date", "-pk").first()
            if snapshot:
                row["as_of"] = snapshot.snapshot_date
                details = snapshot.extra_data if isinstance(snapshot.extra_data, dict) else {}
                if details.get("missing_exchange_rates") or details.get("valuation_errors"):
                    row["note"] = "最新估值缺少汇率或存在错误，金额暂不展示"
                else:
                    rate = resolve_exchange_rate(snapshot.currency, "USD", snapshot.snapshot_date).rate
                    if rate is None:
                        row["note"] = f"缺少 {snapshot.currency}→USD 汇率"
                    else:
                        row["cash"] = (snapshot.total_cash * rate).quantize(Decimal("0.01"))
                        if details.get("complete") is not False:
                            row["nav"] = (snapshot.total_asset * rate).quantize(Decimal("0.01"))
                        row["note"] = "投资组合快照 · 全币种现金折算为美元"
                        if row["nav"] is None:
                            row["note"] += "；净值估算不完整"
        result.append(row)
    return result


@login_required
def index(request):
    family = _request_family(request)
    watch = list(WheelWatchItem.objects.filter(family=family))
    names = {
        symbol: name for symbol, name in Security.objects.filter(
            symbol__in=[item.symbol for item in watch], market__iexact="US",
        ).values_list("symbol", "name")
    }
    for item in watch:
        item.display_name = item.name or names.get(item.symbol) or item.symbol
    jobs = list(WheelAnalysisJob.objects.filter(family=family).order_by("-created_at")[:20])
    counts = {}
    for job in reversed(jobs):
        key = timezone.localtime(job.created_at).date()
        counts[key] = counts.get(key, 0) + 1
        job.day_number = counts[key]
    latest = next((job for job in jobs if job.selection.get("mode") in ("screening_v2", "screening_close_v2")), None)
    visible_results = []
    if latest and latest.status == "saved":
        from .screening import present_results
        visible_results = present_results(latest.screening_results, latest.selection)
    ny_now = timezone.now().astimezone(NY)
    key = uuid4()
    return render(request, "option_wheel/screen_index.html", {
        "accounts": account_summary(family), "watchlist": watch, "jobs": jobs,
        "latest_job": latest,
        "visible_results": visible_results,
        "analysis_request_token": signing.dumps({"family": family.pk, "key": str(key)}, salt="wheel-live-job-v1"),
        "analysis_status_url": reverse("option_wheel:job_status", args=[key]),
        "today_ny": ny_now.date(),
        "default_close": ny_now.weekday() >= 5 or not time(9, 30) <= ny_now.time() < time(16),
    })


@login_required
@require_POST
def watch_action(request):
    family = _request_family(request)
    if not request.user.is_superuser:
        raise PermissionDenied("只有管理员可修改期权自选列表。")
    action = request.POST.get("action")
    if action == "add":
        symbol = request.POST.get("symbol", "").strip().upper().removeprefix("US.")
        if not SYMBOL_PATTERN.fullmatch(symbol):
            return HttpResponseBadRequest("美股代码无效。")
        if WheelWatchItem.objects.filter(family=family, symbol=symbol).exists():
            messages.info(request, f"{symbol} 已在自选列表。")
        elif WheelWatchItem.objects.filter(family=family).count() >= 20:
            messages.error(request, "期权自选列表最多 20 只。")
        else:
            name = Security.objects.filter(symbol__iexact=symbol, market__iexact="US").values_list("name", flat=True).first() or ""
            WheelWatchItem.objects.create(family=family, symbol=symbol, name=name)
            messages.success(request, f"已添加 {symbol}。每日更新后显示价格和事件日期。")
    elif action == "remove":
        symbol = request.POST.get("symbol", "").strip().upper()
        WheelWatchItem.objects.filter(family=family, symbol=symbol).delete()
        messages.success(request, f"已从期权自选列表移除 {symbol}。")
    elif action == "refresh_prices":
        from .watch_refresh import refresh_watch_prices
        try:
            updated = refresh_watch_prices(family)
        except Exception:
            messages.error(request, "价格更新失败，请检查 Futu OpenD；原有价格未改动。")
        else:
            messages.success(request, f"已更新 {updated} 只股票的价格和价格日期；财报及除息信息未改动。")
    else:
        return HttpResponseBadRequest("未知操作。")
    return redirect("option_wheel:index")


@login_required
@require_POST
def analyze(request):
    family = _request_family(request)
    if not request.user.is_superuser:
        raise PermissionDenied("只有管理员可提交期权分析。")
    symbols = sorted(set(value.strip().upper() for value in request.POST.getlist("symbols") if value.strip()))
    if not symbols or len(symbols) > 20:
        return HttpResponseBadRequest("请选择 1–20 只自选股票。")
    try:
        premium_min = Decimal(request.POST.get("premium_min", "100"))
        premium_max = Decimal(request.POST.get("premium_max", "500"))
        if not (premium_min.is_finite() and premium_max.is_finite() and Decimal(0) <= premium_min <= premium_max <= Decimal(100000)):
            raise ValueError
    except (InvalidOperation, TypeError, ValueError):
        return HttpResponseBadRequest("权利金范围无效。")
    today = timezone.now().astimezone(NY).date()
    choice = request.POST.get("expiry_choice", "this")
    try:
        if choice == "custom":
            expiry = date.fromisoformat(request.POST.get("custom_expiry", ""))
        else:
            weeks = {"this": 0, "next": 1, "two": 2}[choice]
            expiry = today + timedelta(days=(4 - today.weekday()) % 7 + 7 * weeks)
    except (KeyError, ValueError):
        return HttpResponseBadRequest("到期日无效。")
    if expiry.weekday() != 4 or not 0 < (expiry - today).days <= 35:
        return HttpResponseBadRequest("请选择未来 35 天内的周五到期日。")
    try:
        token = signing.loads(request.POST.get("request_token", ""), salt="wheel-live-job-v1", max_age=7200)
        if token["family"] != family.pk:
            raise ValueError
        key = UUID(token["key"])
    except (signing.BadSignature, KeyError, TypeError, ValueError):
        return HttpResponseBadRequest("提交凭证无效，请重新打开页面。")
    from .jobs import enqueue, job_payload
    from .analysis_service import WheelAnalysisError
    basis = request.POST.get("analysis_basis", "live")
    if basis not in ("live", "close"):
        return HttpResponseBadRequest("分析依据无效。")
    selection = {
        "mode": "screening_close_v2" if basis == "close" else "screening_v2", "symbols": symbols,
        "target_expiration": expiry.isoformat(), "analysis_date": today.isoformat(),
        "premium_min": str(premium_min), "premium_max": str(premium_max),
        "allow_earnings": request.POST.get("allow_earnings") == "on",
        "allow_dividend": request.POST.get("allow_dividend") == "on",
    }
    try:
        job = enqueue(family, request.user, key, selection)
    except WheelAnalysisError as exc:
        return HttpResponseBadRequest(str(exc))
    if request.headers.get("Accept") == "application/json":
        return JsonResponse(job_payload(job), status=202)
    return redirect("option_wheel:job_detail", pk=job.pk)
