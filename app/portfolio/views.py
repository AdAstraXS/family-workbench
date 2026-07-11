from collections import defaultdict
from datetime import date
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core import signing
from django.db import transaction
from django.db.models import Q, Sum
from django.http import HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_POST

from family_core.audit import stamp_actor
from family_core.household import get_site_setting
from family_core.models import AssetCategory, Currency, FamilyMember
from ledger.models import BankAccount

from .forms import (
    InvestmentAccountForm,
    InvestmentCashMovementForm,
    InvestmentPositionForm,
    InvestmentTransactionForm,
    OptionContractForm,
    SecurityForm,
)
from .futu_service import FutuQueryError, search_futu_securities
from .exchange_rate_service import ensure_daily_exchange_rates
from .models import (
    InvestmentAccount,
    InvestmentCashMovement,
    InvestmentPosition,
    InvestmentTransaction,
    PortfolioSnapshot,
    Security,
    SecurityMarketSnapshot,
    TransactionSourceChoices,
    WatchlistItem,
)
from .services import rebuild_cash_only_transaction, rebuild_position
from .valuation import convert_currency as _convert_currency, value_portfolio


ZERO = Decimal("0")


def _sum_or_none(values):
    values = list(values)
    return sum(values, ZERO) if all(value is not None for value in values) else None


def _visible_accounts(request):
    accounts = InvestmentAccount.objects.select_related(
        "bank_account",
        "bank_account__family",
        "bank_account__member",
        "bank_account__account_region",
    ).filter(
        bank_account__is_active=True,
        bank_account__supports_investment=True,
    )
    if request.user.is_superuser:
        return accounts
    member = FamilyMember.objects.filter(user=request.user, is_active=True).first()
    return accounts.filter(bank_account__family=member.family) if member else accounts.none()


def _latest_positions(accounts, year):
    return list(
        InvestmentPosition.objects.filter(account__in=accounts)
        .select_related(
            "account",
            "account__bank_account__member",
            "account__bank_account__family",
            "security",
            "security__asset_category",
            "security__market_snapshot",
            "security__option_contract",
        )
        .order_by("account__bank_account__member__display_name", "account__bank_account__account_name", "security__symbol")
    )


def _account_dashboard_data(request, account=None):
    accounts = _visible_accounts(request)
    if account:
        accounts = accounts.filter(pk=account.pk)
    accounts = list(
        accounts.order_by("bank_account__member__display_name", "bank_account__account_name")
    )
    family = accounts[0].family if accounts else None
    site_base_currency = get_site_setting().base_currency
    selected_currency = request.GET.get(
        "currency", site_base_currency
    ).upper()
    year_value = request.GET.get("year", str(date.today().year))
    year = int(year_value) if year_value.isdigit() else "all"
    cost_method = request.GET.get("cost_method", "moving_average")
    if cost_method not in {"moving_average", "diluted"}:
        cost_method = "moving_average"
    positions = _latest_positions(accounts, year)

    transaction_filter = InvestmentTransaction.objects.filter(account__in=accounts)
    cash_filter = InvestmentCashMovement.objects.filter(account__in=accounts)
    if year != "all":
        transaction_filter = transaction_filter.filter(trade_date__year=year)

    realized = {
        (row["account_id"], row["security_id"]): row["total"] or ZERO
        for row in transaction_filter.values("account_id", "security_id").annotate(
            total=Sum("realized_pnl")
        )
    }
    cash_by_account = defaultdict(list)
    for row in cash_filter.values("account_id", "currency").annotate(total=Sum("amount")):
        cash_by_account[row["account_id"]].append((row["currency"], row["total"] or ZERO))

    account_rows = []
    positions_by_account = defaultdict(list)
    for item in positions:
        snapshot = getattr(item.security, "market_snapshot", None)
        item.display_price = snapshot.last_price if snapshot and snapshot.last_price is not None else item.current_price
        item.display_cost = (
            item.diluted_cost
            if cost_method == "diluted"
            else item.avg_cost
        )
        multiplier = item.security.contract_multiplier
        item.market_value_live = item.quantity * item.display_price * multiplier
        item.unrealized_live = (
            item.market_value_live - item.quantity * item.display_cost * multiplier
        )
        item.realized_for_year = realized.get((item.account_id, item.security_id), ZERO)
        item.original_currency = item.security.currency
        base_currency = site_base_currency
        item.unrealized_base = _convert_currency(
            item.unrealized_live, item.original_currency, base_currency, date.today()
        )
        item.realized_base = _convert_currency(
            item.realized_for_year, item.original_currency, base_currency, date.today()
        )
        item.market_value_base = _convert_currency(
            item.market_value_live, item.original_currency, base_currency, date.today()
        )
        change_rate = snapshot.change_rate if snapshot else None
        item.today_pnl = (
            item.quantity
            * (item.display_price - item.display_price / (1 + change_rate / 100))
            * multiplier
            if change_rate not in (None, -100)
            else ZERO
        )
        positions_by_account[item.account_id].append(item)

    for item in accounts:
        cash_entries = cash_by_account[item.pk]
        cash_display_values = [
            _convert_currency(amount, currency, selected_currency)
            for currency, amount in cash_entries
        ]
        cash_display = _sum_or_none(cash_display_values)
        item_positions = positions_by_account[item.pk]
        market_values = [
            _convert_currency(p.market_value_live, p.original_currency, selected_currency)
            for p in item_positions
        ]
        market_display = _sum_or_none(market_values)
        unrealized_values = [
            _convert_currency(p.unrealized_live, p.original_currency, selected_currency)
            for p in item_positions
        ]
        realized_values = [
            _convert_currency(p.realized_for_year, p.original_currency, selected_currency)
            for p in item_positions
        ]
        today_values = [
            _convert_currency(p.today_pnl, p.original_currency, selected_currency)
            for p in item_positions
        ]
        row = {
            "account": item,
            "cash": cash_display,
            "market_value": market_display,
            "total_asset": (
                cash_display + market_display
                if cash_display is not None and market_display is not None
                else None
            ),
            "unrealized": _sum_or_none(unrealized_values),
            "realized": _sum_or_none(realized_values),
            "today_pnl": _sum_or_none(today_values),
        }
        row["position_ratio"] = (
            row["market_value"] / row["total_asset"] * 100
            if row["market_value"] is not None and row["total_asset"]
            else ZERO
        )
        row["cash_ratio"] = Decimal("100") - row["position_ratio"]
        account_rows.append(row)

    account_rows.sort(
        key=lambda row: (
            row["total_asset"] is not None,
            row["total_asset"] or ZERO,
        ),
        reverse=True,
    )

    summary_rows = []
    scopes = [("家庭汇总", account_rows)]
    members = []
    for row in account_rows:
        if row["account"].member not in members:
            members.append(row["account"].member)
    scopes.extend(
        (member.display_name, [r for r in account_rows if r["account"].member_id == member.pk])
        for member in members
    )
    for label, rows in scopes:
        summary_rows.append(
            {
                "label": label,
                "cash": _sum_or_none(r["cash"] for r in rows),
                "market_value": _sum_or_none(r["market_value"] for r in rows),
                "total_asset": _sum_or_none(r["total_asset"] for r in rows),
                "today_pnl": _sum_or_none(r["today_pnl"] for r in rows),
                "unrealized": _sum_or_none(r["unrealized"] for r in rows),
                "realized": _sum_or_none(r["realized"] for r in rows),
            }
        )
    return {
        "accounts": accounts,
        "account_rows": account_rows,
        "positions": positions,
        "summary_rows": summary_rows,
        "selected_currency": selected_currency,
        "selected_year": year_value,
        "cost_method": cost_method,
        "currency_options": Currency.objects.filter(is_active=True),
        "year_options": range(date.today().year, date.today().year - 6, -1),
        "family": family,
        "missing_exchange_rates": any(
            row[field] is None
            for row in account_rows
            for field in ("cash", "market_value", "total_asset")
        ),
    }


def save_form(request, form_class, template_name, success_url_name, title, instance=None):
    if request.method == "POST":
        form = form_class(request.POST, instance=instance)
        if form.is_valid():
            item = stamp_actor(form.save(commit=False), request.user)
            item.save()
            form.save_m2m()
            return redirect(success_url_name)
    else:
        form = form_class(instance=instance)
    return render(request, template_name, {"form": form, "title": title})


@login_required
def overview(request):
    login_member = (
        FamilyMember.objects.select_related("family")
        .filter(user=request.user, is_active=True)
        .first()
    )
    if not login_member:
        return render(
            request,
            "portfolio/overview.html",
            {
                "members": FamilyMember.objects.none(),
                "asset_groups": [],
                "trend_snapshots": [],
                "base_currency": "CNY",
            },
        )

    family = login_member.family
    base_currency = get_site_setting().base_currency
    members = FamilyMember.objects.filter(
        family=family,
        is_active=True,
    ).order_by("display_name")
    requested_member = request.GET.get("member")
    if requested_member is None:
        selected_member = login_member
        selected_member_value = str(login_member.pk)
    elif requested_member == "all":
        selected_member = None
        selected_member_value = "all"
    else:
        selected_member = members.filter(pk=requested_member).first() or login_member
        selected_member_value = str(selected_member.pk)

    accounts = _visible_accounts(request)
    if selected_member:
        accounts = accounts.filter(bank_account__member=selected_member)
    accounts = list(accounts)

    today = timezone.localdate()
    valuation = value_portfolio(accounts, base_currency, today)
    positions = valuation["positions"]

    groups = {}
    missing_rates = False

    def add_asset(category, account, amount):
        if amount is None:
            return
        group = groups.setdefault(
            category,
            {"name": category, "amount": ZERO, "accounts": defaultdict(Decimal)},
        )
        group["amount"] += amount
        group["accounts"][account] += amount

    account_map = {item.pk: item for item in accounts}
    total_cash = valuation["total_cash"]
    missing_rates = valuation["missing_rates"]
    for row in valuation["cash_lines"]:
        add_asset("现金", account_map[row["account_id"]], row["converted"])

    total_market_value = valuation["total_market_value"]
    total_cost = valuation["total_cost"]
    asset_type_names = {
        "stock": "股票",
        "fund": "基金",
        "bond": "债券",
        "crypto": "加密资产",
        "other": "其他资产",
    }
    for item in positions:
        market_cny = item.valuation_market_value
        cost_cny = item.valuation_cost
        if market_cny is None or cost_cny is None:
            missing_rates = True
            continue
        category = (
            item.security.asset_category.name
            if item.security.asset_category
            else asset_type_names.get(
                item.security.asset_type,
                item.security.asset_type,
            )
        )
        add_asset(category, item.account, market_cny)

    total_asset = valuation["total_asset"]
    total_pnl = valuation["total_pnl"]

    trend_snapshots = list(
        PortfolioSnapshot.objects.filter(
            family=family,
            member=selected_member,
            account=None,
            currency=base_currency,
        )
        .order_by("-snapshot_date", "-pk")[:60]
    )
    trend_snapshots.reverse()
    values = [item.total_asset for item in trend_snapshots]
    chart_width, chart_height = Decimal("1000"), Decimal("180")
    if values:
        minimum, maximum = min(values), max(values)
        spread = maximum - minimum
        if not spread:
            spread = max(abs(maximum) * Decimal("0.05"), Decimal("1"))
            minimum = maximum - spread / 2
            maximum += spread / 2
        point_count = max(len(values) - 1, 1)
        points = []
        for index, value in enumerate(values):
            x = Decimal(index) / Decimal(point_count) * chart_width
            y = Decimal("15") + (
                (maximum - value) / spread * (chart_height - Decimal("30"))
            )
            points.append(f"{x:.2f},{y:.2f}")
        if len(points) == 1:
            points.append(f"{chart_width:.2f},{points[0].split(',')[1]}")
        trend_points = " ".join(points)
    else:
        trend_points = ""

    change_amount = values[-1] - values[0] if len(values) > 1 else ZERO
    change_ratio = (
        change_amount / values[0] * 100
        if len(values) > 1 and values[0]
        else ZERO
    )

    colors = [
        "#7c3aed",
        "#2563eb",
        "#0891b2",
        "#db2777",
        "#16a34a",
        "#d97706",
        "#475569",
    ]
    asset_groups = []
    for index, group in enumerate(
        sorted(groups.values(), key=lambda item: item["amount"], reverse=True)
    ):
        amount = group["amount"]
        ratio = amount / total_asset * 100 if total_asset else ZERO
        account_rows = []
        for account, account_amount in sorted(
            group["accounts"].items(),
            key=lambda item: item[1],
            reverse=True,
        ):
            account_rows.append(
                {
                    "account": account,
                    "amount": account_amount,
                    "ratio": (
                        account_amount / total_asset * 100
                        if total_asset
                        else ZERO
                    ),
                }
            )
        asset_groups.append(
            {
                "name": group["name"],
                "amount": amount,
                "ratio": ratio,
                "bar_ratio": min(max(ratio, ZERO), Decimal("100")),
                "color": colors[index % len(colors)],
                "accounts": account_rows,
            }
        )

    return render(
        request,
        "portfolio/overview.html",
        {
            "members": members,
            "selected_member": selected_member,
            "selected_member_value": selected_member_value,
            "selected_scope_label": (
                selected_member.display_name
                if selected_member
                else "全部家庭成员"
            ),
            "base_currency": base_currency,
            "total_cash": total_cash,
            "total_market_value": total_market_value,
            "total_asset": total_asset,
            "total_cost": total_cost,
            "total_pnl": total_pnl,
            "asset_groups": asset_groups,
            "trend_snapshots": trend_snapshots,
            "trend_points": trend_points,
            "trend_start": (
                trend_snapshots[0].snapshot_date if trend_snapshots else today
            ),
            "trend_end": (
                trend_snapshots[-1].snapshot_date if trend_snapshots else today
            ),
            "change_amount": change_amount,
            "change_ratio": change_ratio,
            "missing_rates": missing_rates,
        },
    )


@login_required
def account_list(request):
    rate_info = ensure_daily_exchange_rates()
    context = _account_dashboard_data(request)
    context["rate_info"] = rate_info
    return render(
        request,
        "portfolio/account_dashboard.html",
        context,
    )


@login_required
def account_detail(request, pk):
    rate_info = ensure_daily_exchange_rates()
    account = get_object_or_404(_visible_accounts(request), pk=pk)
    context = _account_dashboard_data(request, account)
    context["rate_info"] = rate_info
    context["account"] = account
    context["active_tab"] = request.GET.get("tab", "overview")

    movements = list(
        InvestmentCashMovement.objects.filter(account=account)
        .select_related("transaction", "transaction__security")
        .order_by("movement_date", "created_at", "pk")
    )
    balances = defaultdict(Decimal)
    display_movements = []
    selected_year = context["selected_year"]
    for movement in movements:
        balances[movement.currency] += movement.amount
        movement.balance_after = balances[movement.currency]
        movement.base_balance_after = _convert_currency(
            movement.balance_after,
            movement.currency,
            account.family.base_currency,
            movement.movement_date,
        )
        if selected_year == "all" or str(movement.movement_date.year) == selected_year:
            display_movements.append(movement)
    context["cash_movements"] = list(reversed(display_movements))
    symbols = {
        item.code: item.symbol or item.code
        for item in Currency.objects.filter(is_active=True)
    }
    balance_currencies = ["HKD", "USD", "CNY"] + sorted(
        set(balances) - {"HKD", "USD", "CNY"}
    )
    context["cash_balances"] = [
        {
            "currency": currency,
            "symbol": symbols.get(currency, currency),
            "amount": balances[currency],
        }
        for currency in balance_currencies
    ]

    transactions = InvestmentTransaction.objects.filter(account=account).select_related(
        "security"
    )
    if selected_year != "all":
        transactions = transactions.filter(trade_date__year=selected_year)
    context["transactions"] = list(
        transactions.order_by("-trade_date", "-created_at", "-pk")
    )
    for item in context["transactions"]:
        item.total_fee = item.fee + item.tax
    account_row = context["account_rows"][0] if context["account_rows"] else None
    context["account_row"] = account_row
    for item in context["positions"]:
        converted_market_value = _convert_currency(
            item.market_value_live,
            item.original_currency,
            context["selected_currency"],
        )
        item.position_ratio = (
            converted_market_value / account_row["total_asset"] * 100
            if converted_market_value is not None
            and account_row
            and account_row["total_asset"]
            else ZERO
        )
    return render(request, "portfolio/account_detail.html", context)


@login_required
def account_prototype(request):
    return render(request, "portfolio/account_prototype.html")


@login_required
def account_detail_prototype(request, account_id):
    return render(
        request,
        "portfolio/account_detail_prototype.html",
        {
            "account_id": account_id,
            "active_tab": request.GET.get("tab", "overview"),
        },
    )


@login_required
def account_create(request):
    messages.info(
        request,
        "证券投资账户由账户表中的“券商”账户自动生成，无需手工新增。",
    )
    return redirect("portfolio:account_list")


@login_required
def account_edit(request, pk):
    messages.info(
        request,
        "请到账户管理中修改券商账户，投资账户会自动同步。",
    )
    return redirect("portfolio:account_list")


@login_required
def cash_movement_create(request, account_id):
    account = get_object_or_404(_visible_accounts(request), pk=account_id)
    if request.method == "POST":
        form = InvestmentCashMovementForm(request.POST, account=account)
        if form.is_valid():
            movement = form.save(commit=False)
            movement.account = account
            stamp_actor(movement, request.user)
            movement.save()
            return redirect(f"{account.get_absolute_url()}?tab=cashflows")
    else:
        form = InvestmentCashMovementForm(
            account=account,
            initial={"movement_date": date.today()},
        )
    return render(
        request,
        "form.html",
        {"form": form, "title": f"{account.account_name} · 入金 / 出金"},
    )


@login_required
def security_list(request):
    member = (
        FamilyMember.objects.select_related("family")
        .filter(user=request.user, is_active=True)
        .first()
    )
    query = request.GET.get("q", "").strip()
    market = request.GET.get("market", "HK").strip().upper()
    if market not in {"HK", "US", "CN"}:
        market = "HK"
    search_results = []
    query_error = ""
    if query:
        try:
            search_results = search_futu_securities(query, market)
        except FutuQueryError as exc:
            query_error = str(exc)
        else:
            for item in search_results:
                item["add_token"] = signing.dumps(
                    item,
                    salt="portfolio.watchlist",
                    compress=True,
                )

    watchlist_items = (
        WatchlistItem.objects.select_related(
            "security",
            "security__market_snapshot",
            "member",
        )
        .filter(family=member.family, is_active=True)
        if member
        else WatchlistItem.objects.none()
    )
    watched_ids = set(watchlist_items.values_list("security_id", flat=True))
    watched_keys = set(
        Security.objects.filter(pk__in=watched_ids).values_list("market", "symbol")
    )
    for item in search_results:
        item["is_watched"] = (item["market"], item["symbol"]) in watched_keys
    return render(
        request,
        "portfolio/security_list.html",
        {
            "watchlist_items": watchlist_items,
            "search_results": search_results,
            "query": query,
            "selected_market": market,
            "query_error": query_error,
            "has_member": member is not None,
        },
    )


def _decimal_or_none(value):
    return Decimal(str(value)) if value not in (None, "") else None


@login_required
@require_POST
def watchlist_add(request):
    member = (
        FamilyMember.objects.select_related("family")
        .filter(user=request.user, is_active=True)
        .first()
    )
    if not member:
        messages.error(request, "当前登录用户尚未关联家庭成员，无法添加自选股。")
        return redirect("portfolio:security_list")
    try:
        data = signing.loads(
            request.POST.get("token", ""),
            salt="portfolio.watchlist",
            max_age=900,
        )
    except signing.BadSignature:
        messages.error(request, "查询结果已失效，请重新查询后添加。")
        return redirect("portfolio:security_list")

    security, _ = Security.objects.get_or_create(
        symbol=data["symbol"],
        market=data["market"],
        defaults={"name": data["name"]},
    )
    security.name = data["name"]
    security.exchange = data["exchange"]
    security.asset_type = data["asset_type"]
    security.currency = data["currency"]
    security.lot_size = int(data.get("lot_size") or 0)
    security.listing_date = parse_date(data.get("listing_date") or "")
    security.is_delisted = bool(data.get("is_delisted"))
    security.data_source = "futu"
    security.source_updated_at = timezone.now()
    security.extra_data = {
        **security.extra_data,
        "futu": data.get("raw_data") or {},
    }
    security.save()

    SecurityMarketSnapshot.objects.update_or_create(
        security=security,
        defaults={
            "quote_time": data.get("quote_time") or "",
            "last_price": _decimal_or_none(data.get("last_price")),
            "change_rate": _decimal_or_none(data.get("change_rate")),
            "total_market_value": _decimal_or_none(data.get("total_market_value")),
            "pe_ratio": _decimal_or_none(data.get("pe_ratio")),
            "pe_ttm_ratio": _decimal_or_none(data.get("pe_ttm_ratio")),
            "pb_ratio": _decimal_or_none(data.get("pb_ratio")),
            "ps_ratio": _decimal_or_none(data.get("ps_ratio")),
            "dividend_yield_ttm": _decimal_or_none(data.get("dividend_yield_ttm")),
            "turnover_rate": _decimal_or_none(data.get("turnover_rate")),
            "high_52_week": _decimal_or_none(data.get("high_52_week")),
            "low_52_week": _decimal_or_none(data.get("low_52_week")),
            "issued_shares": data.get("issued_shares"),
            "outstanding_shares": data.get("outstanding_shares"),
            "raw_data": data.get("raw_data") or {},
        },
    )
    item, created = WatchlistItem.objects.update_or_create(
        family=member.family,
        security=security,
        defaults={"member": member, "is_active": True},
    )
    messages.success(
        request,
        f"{security} 已添加到自选股。" if created else f"{security} 已在自选股中。",
    )
    return redirect("portfolio:security_list")


@login_required
def security_create(request):
    return save_form(request, SecurityForm, "portfolio/security_form.html", "portfolio:security_list", "新增证券标的")


@login_required
def security_edit(request, pk):
    security = get_object_or_404(Security, pk=pk)
    return save_form(request, SecurityForm, "portfolio/security_form.html", "portfolio:security_list", "编辑证券标的", security)


@login_required
def option_contract_create(request):
    member = FamilyMember.objects.filter(user=request.user, is_active=True).select_related("family").first()
    if not member:
        messages.error(request, "当前登录用户尚未关联家庭成员。")
        return redirect("portfolio:security_list")
    form = OptionContractForm(
        request.POST or None,
        family=member.family,
    )
    if request.method == "POST" and form.is_valid():
        form.save(member)
        messages.success(request, "期权合约已创建并加入自选标的。")
        return redirect("portfolio:security_list")
    return render(request, "form.html", {"form": form, "title": "新增期权合约"})


@login_required
def position_list(request):
    return HttpResponseRedirect(f"{reverse('portfolio:account_list')}#holdings")


@login_required
def position_create(request):
    messages.info(request, "持仓已改为由交易流水自动计算，无需手工新增。")
    return redirect("portfolio:account_list")


@login_required
def position_edit(request, pk):
    messages.info(request, "持仓已改为由交易流水自动计算，请修改对应交易记录。")
    return redirect("portfolio:account_list")


@login_required
def transaction_list(request):
    transactions = InvestmentTransaction.objects.filter(
        account__in=_visible_accounts(request)
    ).select_related("account", "security").order_by("-trade_date", "-created_at")[:100]
    return render(request, "portfolio/transaction_list.html", {"transactions": transactions})


@login_required
def transaction_create(request):
    return save_transaction_form(request, "新增交易记录")


@login_required
def transaction_edit(request, pk):
    transaction = get_object_or_404(
        InvestmentTransaction.objects.filter(account__in=_visible_accounts(request)),
        pk=pk,
    )
    return save_transaction_form(request, "编辑交易记录", transaction)


@login_required
@require_POST
@transaction.atomic
def transaction_delete(request, pk):
    item = get_object_or_404(
        InvestmentTransaction.objects.filter(account__in=_visible_accounts(request)),
        pk=pk,
    )
    if item.source != TransactionSourceChoices.MANUAL:
        messages.error(request, "同步交易不能在投资组合中删除，请回到来源模块撤销。")
        return redirect(f"{item.account.get_absolute_url()}?tab=transactions")
    account = item.account
    security = item.security
    item.delete()
    if security:
        rebuild_position(account, security)
    messages.success(request, "交易记录已删除，持仓和现金流水已重新计算。")
    return redirect(f"{account.get_absolute_url()}?tab=transactions")


@login_required
def transaction_form_options(request):
    family_id = request.GET.get("family", "")
    member_id = request.GET.get("member", "")
    login_member = FamilyMember.objects.filter(
        user=request.user,
        is_active=True,
    ).first()
    family_allowed = request.user.is_superuser or (
        login_member and str(login_member.family_id) == str(family_id)
    )
    if not family_allowed:
        return JsonResponse(
            {
                "members": [],
                "accounts": [],
                "categories": [],
                "securities": [],
            }
        )
    members = FamilyMember.objects.filter(
        family_id=family_id,
        is_active=True,
    ).order_by("display_name")
    accounts = BankAccount.objects.filter(
        family_id=family_id,
        is_active=True,
        supports_investment=True,
    )
    if member_id:
        accounts = accounts.filter(member_id=member_id)
    categories = AssetCategory.objects.filter(
        Q(family_id=family_id) | Q(family=None),
        is_active=True,
    ).order_by("display_order", "name")
    securities = Security.objects.filter(
        watchlist_items__family_id=family_id,
        watchlist_items__is_active=True,
    ).distinct().order_by("market", "symbol")
    return JsonResponse(
        {
            "members": [{"id": item.pk, "name": item.display_name} for item in members],
            "accounts": [
                {
                    "id": item.pk,
                    "name": item.account_name,
                }
                for item in accounts
            ],
            "categories": [{"id": item.pk, "name": item.name} for item in categories],
            "securities": [
                {
                    "id": item.pk,
                    "name": f"{item.symbol} {item.name}",
                    "currency": item.currency,
                    "asset_type": item.asset_type,
                    "multiplier": str(item.contract_multiplier),
                }
                for item in securities
            ],
        }
    )


def save_transaction_form(request, title, instance=None):
    old_pair = (
        (instance.account_id, instance.security_id)
        if instance and instance.security_id
        else None
    )
    if request.method == "POST":
        form = InvestmentTransactionForm(
            request.POST,
            instance=instance,
            user=request.user,
        )
        if form.is_valid():
            try:
                with transaction.atomic():
                    stamp_actor(form.instance, request.user)
                    item = form.save()
                    pairs = {old_pair, (item.account_id, item.security_id)}
                    for account_id, security_id in {
                        pair for pair in pairs if pair and pair[1]
                    }:
                        account = InvestmentAccount.objects.select_related("bank_account").get(pk=account_id)
                        security = Security.objects.get(pk=security_id)
                        rebuild_position(account, security)
                    if not item.security_id:
                        rebuild_cash_only_transaction(item)
            except ValidationError as exc:
                form.add_error(None, exc)
            else:
                return redirect(f"{item.account.get_absolute_url()}?tab=transactions")
    else:
        initial = {}
        if request.GET.get("account", "").isdigit():
            selected_account = _visible_accounts(request).filter(
                pk=request.GET["account"]
            ).first()
            if selected_account:
                initial.update(
                    {
                        "account": selected_account,
                        "family": selected_account.family,
                        "member": selected_account.member,
                        "bank_account": selected_account.bank_account,
                    }
                )
        form = InvestmentTransactionForm(
            instance=instance,
            initial=initial,
            user=request.user,
        )
    return render(
        request,
        "portfolio/transaction_form.html",
        {"form": form, "title": title},
    )
