from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone

from family_core.household import get_household_family
from family_core.models import FamilyMember
from portfolio.models import InvestmentAccount, Security

from .account_capacity import (
    CapacityImportError,
    build_portfolio_capacity,
    import_portfolio_capacity,
)
from .models import (
    DataStatus,
    DelayStatus,
    EventStatus,
    Freshness,
    TechnicalStatus,
    WheelBrokerAccountSnapshot,
    WheelCandidate,
    WheelDecision,
    WheelMarketSnapshot,
    WheelPolicy,
)


PARTICIPATING_ACCOUNTS = ("致富证券（公户）", "盈透证券")
WATCHLIST_SYMBOLS = (
    "TSLA",
    "MSFT",
    "AAPL",
    "AMZN",
    "GOOG",
    "GOOGL",
    "META",
    "NVDA",
    "TSM",
    "ASML",
    "AMD",
    "INTC",
    "MU",
    "SKHY",
    "SPCX",
)


def _request_family(request):
    member = (
        FamilyMember.objects.select_related("family")
        .filter(user=request.user, is_active=True)
        .first()
    )
    if member:
        return member.family
    if request.user.is_superuser:
        family = get_household_family()
        if family:
            return family
    raise PermissionDenied("当前用户未关联有效家庭。")


def _snapshot_is_ready(snapshot, *, max_age_minutes, now):
    if snapshot is None or snapshot.data_status != DataStatus.COMPLETE:
        return False
    required_amounts = (
        snapshot.settled_cash,
        snapshot.unsettled_cash,
        snapshot.nav,
        snapshot.reserved_cash,
        snapshot.margin_loan_balance,
    )
    if any(value is None or not value.is_finite() for value in required_amounts):
        return False
    if (
        snapshot.currency != "USD"
        or not snapshot.source_reference.strip()
        or snapshot.nav <= 0
        or snapshot.reserved_cash > snapshot.settled_cash
        or snapshot.uses_margin is not False
        or snapshot.margin_loan_balance != Decimal("0")
        or not isinstance(snapshot.positions_summary, dict)
        or not isinstance(snapshot.open_obligations, dict)
    ):
        return False
    age = now - snapshot.source_as_of
    return timedelta(0) <= age <= timedelta(minutes=max_age_minutes)


def _latest_by_underlying(queryset):
    latest = {}
    for snapshot in queryset.order_by("underlying_id", "-source_as_of", "-pk"):
        latest.setdefault(snapshot.underlying_id, snapshot)
    return latest


def _event_evidence(decision, prefix):
    unknown = {
        "status": "未知",
        "as_of": None,
        "detail": "尚无独立持久化证据",
        "is_verified": False,
    }
    if decision is None or not isinstance(decision.frozen_input, dict):
        return unknown
    status = decision.frozen_input.get(f"{prefix}_status")
    as_of = decision.frozen_input.get(f"{prefix}_as_of")
    if status in (None, "") or as_of in (None, ""):
        return unknown
    return {
        "status": str(status),
        "as_of": str(as_of),
        "detail": "来自最近一次冻结决策输入",
        "is_verified": True,
    }


def _select_participating_account(matches):
    owner_matches = [
        account
        for account in matches
        if account.bank_account.member
        and account.bank_account.member.display_name.strip() == "我"
    ]
    if len(owner_matches) == 1:
        return owner_matches[0], False

    evidenced_matches = [
        account
        for account in matches
        if WheelBrokerAccountSnapshot.objects.filter(account=account).exists()
    ]
    if len(evidenced_matches) == 1:
        return evidenced_matches[0], False
    if len(matches) == 1:
        return matches[0], False
    return None, len(matches) > 1


@login_required
def index(request):
    family = _request_family(request)
    now = timezone.now()

    policies = list(
        WheelPolicy.objects.filter(family=family)
        .select_related("account__bank_account", "underlying")
        .order_by("underlying__symbol", "account__bank_account__account_name")
    )
    policy_by_symbol = {}
    for policy in policies:
        policy.display_max_underlying_nav_percent = (
            policy.max_underlying_nav_ratio * Decimal("100")
        )
        policy_by_symbol.setdefault(policy.underlying.symbol.upper(), []).append(policy)

    accounts_by_name = {name: [] for name in PARTICIPATING_ACCOUNTS}
    for account in InvestmentAccount.objects.filter(
        bank_account__family=family,
        bank_account__is_active=True,
        bank_account__supports_investment=True,
        bank_account__account_name__in=PARTICIPATING_ACCOUNTS,
    ).select_related("bank_account", "bank_account__member"):
        accounts_by_name[account.account_name].append(account)
    account_cards = []
    account_blockers = []
    for account_name in PARTICIPATING_ACCOUNTS:
        matches = accounts_by_name[account_name]
        account, ambiguous = _select_participating_account(matches)
        snapshot = None
        if account is not None:
            snapshot = (
                WheelBrokerAccountSnapshot.objects.filter(
                    family=family,
                    account=account,
                )
                .order_by("-source_as_of", "-pk")
                .first()
            )
        policy_ages = [
            policy.account_snapshot_max_age_minutes
            for policy in policies
            if policy.account_id == getattr(account, "pk", None)
        ]
        max_age_minutes = min(policy_ages) if policy_ages else 1440
        ready = _snapshot_is_ready(
            snapshot,
            max_age_minutes=max_age_minutes,
            now=now,
        )
        if ambiguous:
            state = "身份有重名"
            account_blockers.append(f"{account_name}存在同名账户，需先生成对应账户的容量快照")
        elif account is None:
            state = "未配置"
            account_blockers.append(f"{account_name}尚未映射到投资账户")
        elif snapshot is None:
            state = "数据不可用"
            account_blockers.append(f"{account_name}尚无容量快照")
        elif not ready:
            state = "证据不完整或已过期"
            account_blockers.append(f"{account_name}容量证据未就绪")
        else:
            state = "就绪"
        account_cards.append(
            {
                "name": account_name,
                "account": account,
                "snapshot": snapshot,
                "ready": ready,
                "state": state,
                "max_age_minutes": max_age_minutes,
                "preview": None,
                "preview_error": "",
                "confirm_no_margin": False,
                "confirm_no_open_orders": False,
                "confirm_save_snapshot": False,
            }
        )

    if request.method == "POST":
        action = request.POST.get("action")
        if action not in {"preview_capacity", "save_capacity"}:
            return HttpResponseBadRequest("未知的账户容量操作。")
        if action == "save_capacity" and not request.user.is_superuser:
            raise PermissionDenied("只有管理员可以保存正式账户容量快照。")
        try:
            account_id = int(request.POST.get("account_id", ""))
        except (TypeError, ValueError):
            return HttpResponseBadRequest("账户参数无效。")
        target_card = next(
            (
                card
                for card in account_cards
                if getattr(card["account"], "pk", None) == account_id
            ),
            None,
        )
        if target_card is None:
            raise PermissionDenied("该账户不属于当前家庭的车轮参与账户。")
        target_card["confirm_no_margin"] = (
            request.POST.get("confirm_no_margin") == "yes"
        )
        target_card["confirm_no_open_orders"] = (
            request.POST.get("confirm_no_open_orders") == "yes"
        )
        target_card["confirm_save_snapshot"] = (
            request.POST.get("confirm_save_snapshot") == "yes"
        )
        try:
            evidence = build_portfolio_capacity(
                account_id=account_id,
                confirm_no_margin=target_card["confirm_no_margin"],
                confirm_no_open_orders=target_card["confirm_no_open_orders"],
            )
        except CapacityImportError as exc:
            target_card["preview_error"] = str(exc)
        else:
            target_card["preview"] = {
                "evidence": evidence,
                "available_cash": evidence.settled_cash - evidence.reserved_cash,
                "position_count": evidence.positions_summary.get("count", 0),
                "obligation_count": evidence.open_obligations.get("count", 0),
            }
            if action == "save_capacity":
                if not target_card["confirm_save_snapshot"]:
                    target_card["preview_error"] = (
                        "保存前必须再次确认将当前预演结果写入正式容量快照。"
                    )
                else:
                    result = import_portfolio_capacity(evidence=evidence, commit=True)
                    if result.snapshot_created:
                        messages.success(
                            request,
                            f"已保存 {target_card['name']} 的正式容量快照；"
                            "投资组合、现金、持仓和订单均未修改。",
                        )
                    else:
                        messages.success(
                            request,
                            f"{target_card['name']} 的同一份容量证据已存在，"
                            "本次没有重复写入。",
                        )
                    return redirect(reverse("option_wheel:index"))

    decisions = WheelDecision.objects.filter(family=family)
    latest_decision = (
        decisions.select_related(
            "account__bank_account",
            "underlying",
            "market_snapshot",
        )
        .order_by("-decision_time", "-pk")
        .first()
    )
    decision_blockers = []
    if latest_decision is None:
        decision_blockers.append("尚无冻结决策证据")
    else:
        market = latest_decision.market_snapshot
        market_ready = (
            market.data_quality == DataStatus.COMPLETE
            and market.delay_status == DelayStatus.REAL_TIME
            and market.freshness_status == Freshness.FRESH
            and market.regular_session_verified is True
            and market.market_session.strip().lower() in {"normal", "regular"}
        )
        if not market_ready:
            decision_blockers.append("最近决策的行情尚未完成实时新鲜度核验")
        if latest_decision.event_status != EventStatus.CLEAR:
            decision_blockers.append("最近决策的综合事件门控尚未通过")
        if latest_decision.technical_status != TechnicalStatus.COMPLETE:
            decision_blockers.append("最近决策的技术证据尚不完整")

    family_underlying_ids = {
        policy.underlying_id for policy in policies
    } | set(decisions.values_list("underlying_id", flat=True))
    securities = {
        security.symbol.upper(): security
        for security in Security.objects.filter(
            symbol__in=WATCHLIST_SYMBOLS,
            market__iexact="US",
        ).order_by("symbol", "pk")
    }
    market_by_underlying = _latest_by_underlying(
        WheelMarketSnapshot.objects.filter(
            underlying_id__in=family_underlying_ids,
        ).select_related("underlying")
    )
    watchlist = []
    for symbol in WATCHLIST_SYMBOLS:
        security = securities.get(symbol)
        symbol_policies = policy_by_symbol.get(symbol, [])
        market_snapshot = (
            market_by_underlying.get(security.pk) if security is not None else None
        )
        watchlist.append(
            {
                "symbol": symbol,
                "security": security,
                "policies": symbol_policies,
                "market_snapshot": market_snapshot,
            }
        )

    candidates = list(
        WheelCandidate.objects.filter(decision__family=family)
        .select_related(
            "decision__account__bank_account",
            "decision__underlying",
            "option_quote",
        )
        .order_by("-decision__decision_time", "-created_at", "-pk")[:20]
    )
    for candidate in candidates:
        quote_probability = (
            candidate.option_quote.assignment_probability
            if candidate.option_quote_id
            else None
        )
        candidate.display_assignment_probability = (
            candidate.assignment_probability
            if candidate.assignment_probability is not None
            else quote_probability
        )
        candidate.display_annualized_premium_rate = (
            candidate.annualized_premium_rate * Decimal("100")
            if candidate.annualized_premium_rate is not None
            else None
        )
        reasons = candidate.exclusion_reasons
        if isinstance(reasons, list):
            candidate.display_exclusion_reasons = reasons
        elif reasons in (None, ""):
            candidate.display_exclusion_reasons = []
        else:
            candidate.display_exclusion_reasons = [reasons]

    readiness_blockers = account_blockers + decision_blockers
    data_ready = not readiness_blockers
    context = {
        "execution_enabled": bool(settings.OPTION_WHEEL_EXECUTION_ENABLED),
        "data_ready": data_ready,
        "readiness_blockers": readiness_blockers,
        "account_cards": account_cards,
        "policies": policies,
        "watchlist": watchlist,
        "candidates": candidates,
        "latest_decision": latest_decision,
        "earnings_evidence": _event_evidence(latest_decision, "earnings"),
        "dividend_evidence": _event_evidence(latest_decision, "dividend"),
    }
    return render(request, "option_wheel/index.html", context)
