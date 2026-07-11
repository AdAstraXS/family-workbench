from collections import defaultdict
from datetime import date
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import F, Max, Q, Sum
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils import timezone

from family_core.audit import stamp_actor
from .forms import HkIpoAllotmentForm, HkIpoListingForm, HkIpoSaleForm, HkIpoSubscriptionTradeForm
from .models import HkIpoListing, HkIpoSubscriptionTrade
from .services import (
    IpoImageRecognitionError,
    fetch_vbkr_expected_margin_multiples,
    get_cached_vbkr_expected_margin_multiples,
    get_vision_providers,
    refresh_hk_connect_threshold,
    refresh_listed_market_data,
    recognize_ipo_listing_from_image,
)
from ledger.models import BankAccount
from portfolio.ipo_sync import sync_ipo_trade
from portfolio.models import (
    InvestmentOption,
    InvestmentTransaction,
    TradeStatusChoices,
    TradeTypeChoices,
    TransactionSourceChoices,
)
from portfolio.services import rebuild_position


def load_current_ipo_listings():
    listings = list(
        HkIpoListing.objects.order_by(
            "listing_date",
            "subscription_end_date",
            "stock_code",
            "stock_name",
        )
    )
    changed = []
    for listing in listings:
        if listing.subscription_status == HkIpoListing.STATUS_LISTED:
            continue
        status = listing.calculate_subscription_status()
        if listing.subscription_status != status:
            listing.subscription_status = status
            changed.append(listing)
    if changed:
        HkIpoListing.objects.bulk_update(changed, ["subscription_status"])
    collision_counts = defaultdict(int)
    for listing in listings:
        if (
            listing.subscription_status != HkIpoListing.STATUS_LISTED
            and listing.subscription_end_date
        ):
            collision_counts[listing.subscription_end_date] += 1
    for listing in listings:
        if listing.subscription_status != HkIpoListing.STATUS_LISTED:
            listing._collision_count_cache = collision_counts.get(
                listing.subscription_end_date,
                0,
            )
    return listings


def ipo_profit_date(trade):
    return trade.sell_date or trade.listing.subscription_end_date


def ipo_profit_queryset(queryset):
    return queryset.filter(
        Q(trade_status__in=HkIpoSubscriptionTrade.TERMINAL_STATUSES)
        | Q(sold_lots__gt=0)
    )


def decorate_ipo_rows(trades, *, holding=False):
    for trade in trades:
        if holding:
            remaining_lots = max((trade.allotted_lots or 0) - (trade.sold_lots or 0), 0)
            trade.display_remaining_lots = remaining_lots
            trade.display_holding_value = Decimal(remaining_lots) * (
                trade.listing.entry_fee
                or (trade.listing.final_price or Decimal("0")) * (trade.listing.lot_size or 0)
            )
    return trades


def ipo_sale_transactions(ipo_trade_ids):
    return list(
        InvestmentTransaction.objects.select_related("account", "security")
        .filter(
            source=TransactionSourceChoices.IMPORT,
            trade_type=TradeTypeChoices.SELL,
            external_id__startswith="ipo:",
            extra_data__ipo_subscription_trade_id__in=ipo_trade_ids,
        )
        .order_by(F("trade_date").desc(nulls_last=True), "-created_at", "-pk")
    )


def save_ipo_sale_transaction(ipo_trade, form, transaction=None, user=None):
    sync_ipo_trade(ipo_trade.pk)
    buy_transaction = InvestmentTransaction.objects.filter(
        source=TransactionSourceChoices.IMPORT,
        external_id=f"ipo:{ipo_trade.pk}:buy",
    ).first()
    if not buy_transaction:
        return None
    lot_size = ipo_trade.listing.lot_size or 0
    sold_lots = form.cleaned_data["sold_lots"]
    quantity = Decimal(sold_lots * lot_size)
    if not transaction:
        serial = (
            InvestmentTransaction.objects.filter(
                source=TransactionSourceChoices.IMPORT,
                external_id__startswith=f"ipo:{ipo_trade.pk}:sell:",
            ).count()
            + 1
        )
        transaction = InvestmentTransaction(
            source=TransactionSourceChoices.IMPORT,
            external_id=f"ipo:{ipo_trade.pk}:sell:{serial}",
        )
    transaction.account = buy_transaction.account
    transaction.security = buy_transaction.security
    transaction.asset_category = buy_transaction.asset_category
    transaction.trade_date = form.cleaned_data["sell_date"]
    transaction.trade_type = TradeTypeChoices.SELL
    transaction.trade_type_option = InvestmentOption.objects.filter(
        category=InvestmentOption.CATEGORY_TRANSACTION_TYPE,
        code=TradeTypeChoices.SELL,
        is_active=True,
    ).first()
    transaction.status = TradeStatusChoices.COMPLETED
    transaction.quantity = quantity
    transaction.price = form.cleaned_data["sell_price"]
    transaction.amount = quantity * transaction.price
    transaction.fee = form.cleaned_data["trading_fee"]
    transaction.tax = Decimal("0")
    transaction.currency = buy_transaction.currency
    transaction.remark = ipo_trade.remark
    transaction.extra_data = {"ipo_subscription_trade_id": ipo_trade.pk}
    if user:
        stamp_actor(transaction, user)
    transaction.save()
    rebuild_position(transaction.account, transaction.security)
    refresh_ipo_sale_summary(ipo_trade)
    return transaction


def refresh_ipo_sale_summary(ipo_trade):
    lot_size = ipo_trade.listing.lot_size or 0
    sold_total = (
        InvestmentTransaction.objects.filter(
            source=TransactionSourceChoices.IMPORT,
            trade_type=TradeTypeChoices.SELL,
            extra_data__ipo_subscription_trade_id=ipo_trade.pk,
        ).aggregate(total=Sum("quantity"))["total"]
        or Decimal("0")
    )
    realized_profit = (
        InvestmentTransaction.objects.filter(
            source=TransactionSourceChoices.IMPORT,
            trade_type=TradeTypeChoices.SELL,
            extra_data__ipo_subscription_trade_id=ipo_trade.pk,
        ).aggregate(total=Sum("realized_pnl"))["total"]
        or Decimal("0")
    )
    total_lots = int(sold_total / Decimal(lot_size)) if lot_size else 0
    status = (
        HkIpoSubscriptionTrade.STATUS_CLOSED
        if ipo_trade.allotted_lots and total_lots >= ipo_trade.allotted_lots
        else HkIpoSubscriptionTrade.STATUS_HOLDING
    )
    HkIpoSubscriptionTrade.objects.filter(pk=ipo_trade.pk).update(
        sold_lots=total_lots,
        realized_profit=realized_profit,
        trade_status=status,
    )


def build_ipo_chart_data(trades, selected_year, current_year):
    stock_totals = defaultdict(Decimal)
    account_totals = defaultdict(Decimal)
    trend_totals = defaultdict(Decimal)

    for trade in trades:
        profit = trade.realized_profit or Decimal("0")
        stock_label = (
            f"{trade.listing.stock_name or trade.listing.company_name} "
            f"({trade.listing.stock_code})"
        )
        stock_totals[stock_label] += profit
        account_label = (
            f"{trade.account.member} - {trade.account.account_name}"
            if trade.account
            else "未关联账户"
        )
        account_totals[account_label] += profit
        attribution_date = ipo_profit_date(trade)
        if attribution_date:
            trend_key = (
                attribution_date.year
                if selected_year == "all"
                else attribution_date.month
            )
            trend_totals[trend_key] += profit

    def sorted_series(totals):
        return [
            {"label": label, "value": float(value)}
            for label, value in sorted(
                totals.items(),
                key=lambda item: (-item[1], item[0]),
            )
        ]

    if selected_year == "all":
        if trend_totals:
            trend_keys = list(range(min(trend_totals), max(trend_totals) + 1))
        else:
            trend_keys = [current_year]
        trend_labels = [f"{year}年" for year in trend_keys]
    else:
        trend_keys = list(range(1, 13))
        trend_labels = [f"{month}月" for month in trend_keys]

    return {
        "stock": sorted_series(stock_totals),
        "account": sorted_series(account_totals),
        "trend": {
            "labels": trend_labels,
            "values": [float(trend_totals.get(key, Decimal("0"))) for key in trend_keys],
        },
    }


@login_required
def index(request):
    today = timezone.localdate()
    current_year = today.year
    listings = load_current_ipo_listings()
    trade_queryset = HkIpoSubscriptionTrade.objects.select_related(
        "listing",
        "member",
        "account",
        "account__member",
    )
    available_years = sorted(
        {
            item.subscription_end_date.year
            for item in listings
            if item.subscription_end_date
        }
        | {
            value.year
            for value in trade_queryset.values_list("sell_date", flat=True)
            if value
        },
        reverse=True,
    )
    selected_year = request.GET.get("year", "").strip()
    if not selected_year:
        selected_year = str(request.session.get("ipo_overview_year", "")).strip()
    if not selected_year:
        selected_year = str(current_year) if current_year in available_years else "all"
    if selected_year != "all" and (
        not selected_year.isdigit() or int(selected_year) not in available_years
    ):
        selected_year = "all"
    request.session["ipo_overview_year"] = selected_year

    metric_listings = listings
    trade_metric_queryset = trade_queryset
    profit_queryset = ipo_profit_queryset(trade_queryset)
    if selected_year != "all":
        year = int(selected_year)
        metric_listings = [
            item
            for item in listings
            if item.subscription_end_date
            and item.subscription_end_date.year == year
        ]
        trade_metric_queryset = trade_metric_queryset.filter(
            listing__subscription_end_date__year=year
        )
        profit_queryset = profit_queryset.filter(
            Q(sell_date__year=year)
            | Q(
                sell_date__isnull=True,
                listing__subscription_end_date__year=year,
            )
        )

    today_listings = [
        item
        for item in metric_listings
        if item.subscription_status == HkIpoListing.STATUS_LISTING_TODAY
    ]
    grey_market_listings = [
        item for item in metric_listings if item.allotment_result_date == today
    ]
    grey_market_listing_ids = {item.pk for item in grey_market_listings}
    subscribing_listings = [
        item
        for item in metric_listings
        if item.subscription_status == HkIpoListing.STATUS_SUBSCRIBING
    ]
    waiting_listings = [
        item
        for item in metric_listings
        if item.subscription_status == HkIpoListing.STATUS_WAITING_LISTING
        and item.pk not in grey_market_listing_ids
    ]
    closed_trade_count = (
        trade_metric_queryset.filter(
            Q(trade_status=HkIpoSubscriptionTrade.STATUS_CLOSED)
            | Q(sold_lots__gt=0),
            allotted_lots__gt=0,
        )
        .values("listing_id")
        .distinct()
        .count()
    )
    realized_profit_total = (
        profit_queryset.aggregate(total=Sum("realized_profit"))["total"]
        or Decimal("0")
    )
    chart_data = build_ipo_chart_data(
        list(profit_queryset),
        selected_year,
        current_year,
    )
    return render(
        request,
        "ipo/overview.html",
        {
            "metrics": {
                "listing_today": len(today_listings),
                "grey_market_today": len(grey_market_listings),
                "subscribing": len(subscribing_listings),
                "waiting_listing": len(waiting_listings),
                "listing_total": len(metric_listings),
                "trade_applied": trade_metric_queryset.values("listing_id")
                .distinct()
                .count(),
                "trade_allotted": trade_metric_queryset.filter(
                    allotted_lots__gt=0
                )
                .values("listing_id")
                .distinct()
                .count(),
                "trade_holding": trade_metric_queryset.filter(
                    trade_status=HkIpoSubscriptionTrade.STATUS_HOLDING
                )
                .values("listing_id")
                .distinct()
                .count(),
                "trade_closed": closed_trade_count,
                "realized_profit_total": realized_profit_total,
            },
            "year_filter": {
                "available_years": available_years,
                "selected_year": selected_year,
            },
            "chart_data": chart_data,
        },
    )


@login_required
def listing_list(request):
    listings = load_current_ipo_listings()
    available_years = sorted(
        {
            item.subscription_end_date.year
            for item in listings
            if item.subscription_end_date
        },
        reverse=True,
    )
    selected_year = request.GET.get("year", "").strip()
    if not selected_year:
        selected_year = str(request.session.get("ipo_listing_year", "")).strip()
    if not selected_year:
        current_year = timezone.localdate().year
        selected_year = (
            str(current_year) if current_year in available_years else "all"
        )
    if selected_year != "all" and (
        not selected_year.isdigit() or int(selected_year) not in available_years
    ):
        selected_year = "all"
    request.session["ipo_listing_year"] = selected_year
    if selected_year != "all":
        year = int(selected_year)
        listings = [
            item
            for item in listings
            if item.subscription_end_date
            and item.subscription_end_date.year == year
        ]

    today = timezone.localdate()
    today_listings = [item for item in listings if item.subscription_status == HkIpoListing.STATUS_LISTING_TODAY]
    grey_market_listings = [item for item in listings if item.allotment_result_date == today]
    grey_market_listing_ids = {item.pk for item in grey_market_listings}
    subscribing_listings = [item for item in listings if item.subscription_status == HkIpoListing.STATUS_SUBSCRIBING]
    waiting_listings = [
        item
        for item in listings
        if item.subscription_status == HkIpoListing.STATUS_WAITING_LISTING and item.pk not in grey_market_listing_ids
    ]
    listed_listings = [item for item in listings if item.subscription_status == HkIpoListing.STATUS_LISTED]
    if selected_year != "all":
        market_data_year = int(selected_year)
    else:
        listed_years = [
            (item.listing_date or item.subscription_end_date).year
            for item in listed_listings
            if item.listing_date or item.subscription_end_date
        ]
        market_data_year = (
            (min(listed_years), max(listed_years))
            if listed_years
            else today.year
        )
    refresh_listed_market_data(listed_listings, market_data_year)

    def active_sort_key(item):
        return (item.subscription_end_date or item.listing_date or date.max, item.stock_code, item.stock_name)

    def listed_sort_key(item):
        sort_date = item.listing_date or item.subscription_end_date or date.min
        return (sort_date, item.stock_code, item.stock_name)

    today_listings.sort(key=active_sort_key)
    grey_market_listings.sort(key=active_sort_key)
    subscribing_listings.sort(key=active_sort_key)
    waiting_listings.sort(key=active_sort_key)
    listed_listings.sort(key=listed_sort_key, reverse=True)

    expected_margin_map = get_cached_vbkr_expected_margin_multiples()
    for listing in subscribing_listings + waiting_listings:
        stock_code = (listing.stock_code or "").strip().upper()
        listing.expected_margin_multiple = (
            expected_margin_map.get(stock_code)
            or expected_margin_map.get(stock_code.replace(".HK", ""))
            or "-"
        )

    listed_visible = listed_listings[:10]
    listed_hidden = listed_listings[10:]
    metrics = {
        "listing_today": len(today_listings),
        "grey_market_today": len(grey_market_listings),
        "subscribing": len(subscribing_listings),
        "waiting_listing": len(waiting_listings),
        "total": len(listings),
    }
    return render(
        request,
        "ipo/listing_list.html",
        {
            "metrics": metrics,
            "today_listings": today_listings,
            "grey_market_listings": grey_market_listings,
            "subscribing_listings": subscribing_listings,
            "waiting_listings": waiting_listings,
            "listed_visible": listed_visible,
            "listed_hidden": listed_hidden,
            "listed_count": len(listed_listings),
            "current_date": today,
            "year_filter": {
                "available_years": available_years,
                "selected_year": selected_year,
            },
        },
    )


@login_required
def listing_detail(request, pk):
    listing = get_object_or_404(HkIpoListing, pk=pk)
    return render(request, "ipo/listing_detail.html", {"listing": listing})


@login_required
def expected_margin_data(request):
    if request.method != "GET":
        return JsonResponse({"ok": False, "error": "只支持 GET 请求。"}, status=405)
    return JsonResponse(
        {"ok": True, "data": fetch_vbkr_expected_margin_multiples()}
    )


@login_required
def save_listing_form(request, title, instance=None):
    if request.method == "POST":
        form = HkIpoListingForm(request.POST, request.FILES, instance=instance)
        if form.is_valid():
            listing = form.save(commit=False)
            threshold = refresh_hk_connect_threshold()
            if threshold is not None:
                listing.hk_connect_threshold_100m = threshold
            listing.save()
            form.save_m2m()
            return redirect("ipo:listing_detail", pk=listing.pk)
    else:
        form = HkIpoListingForm(instance=instance)
    return render(
        request,
        "ipo/listing_form.html",
        {
            "form": form,
            "title": title,
            "vision_providers": get_vision_providers(),
        },
    )


@login_required
def listing_create(request):
    return save_listing_form(request, "新增新股资料")


@login_required
def listing_edit(request, pk):
    listing = get_object_or_404(HkIpoListing, pk=pk)
    return save_listing_form(request, "编辑新股资料", listing)


@login_required
def subscription_trade_list(request):
    current_year = timezone.localdate().year
    trade_queryset = HkIpoSubscriptionTrade.objects.select_related("listing", "member", "account")
    subscription_years = {
        value.year
        for value in trade_queryset.values_list(
            "listing__subscription_end_date", flat=True
        )
        if value
    }
    sell_years = {
        value.year for value in trade_queryset.values_list("sell_date", flat=True) if value
    }
    available_years = sorted(
        subscription_years | sell_years,
        reverse=True,
    )
    selected_year = request.GET.get("year", "").strip()
    if not selected_year:
        selected_year = str(request.session.get("ipo_subscription_year", "")).strip()
    if not selected_year:
        selected_year = str(current_year) if current_year in available_years else "all"
    if selected_year != "all" and (
        not selected_year.isdigit() or int(selected_year) not in available_years
    ):
        selected_year = "all"
    request.session["ipo_subscription_year"] = selected_year
    search_query = request.GET.get("q", "").strip()
    selected_status = request.GET.get("status", "").strip()
    valid_statuses = {choice[0] for choice in HkIpoSubscriptionTrade.STATUS_CHOICES}
    if selected_status not in valid_statuses:
        selected_status = ""

    metric_queryset = HkIpoSubscriptionTrade.objects.all()
    profit_queryset = ipo_profit_queryset(HkIpoSubscriptionTrade.objects.all())
    active_queryset = trade_queryset.exclude(
        trade_status__in=HkIpoSubscriptionTrade.TERMINAL_STATUSES
    )
    closed_queryset = trade_queryset.filter(
        Q(trade_status__in=HkIpoSubscriptionTrade.TERMINAL_STATUSES)
        | Q(sold_lots__gt=0)
    )
    if selected_year != "all":
        year = int(selected_year)
        metric_queryset = metric_queryset.filter(
            listing__subscription_end_date__year=year
        )
        profit_queryset = profit_queryset.filter(
            Q(sell_date__year=year)
            | Q(
                sell_date__isnull=True,
                listing__subscription_end_date__year=year,
            )
        )
        active_queryset = active_queryset.filter(
            listing__subscription_end_date__year=year
        )
        closed_queryset = closed_queryset.filter(
            Q(sell_date__year=year)
            | Q(
                sell_date__isnull=True,
                listing__subscription_end_date__year=year,
            )
        )
    if selected_status:
        active_queryset = active_queryset.filter(trade_status=selected_status)
        closed_queryset = closed_queryset.filter(trade_status=selected_status)
    if search_query:
        search_filter = (
            Q(listing__stock_code__icontains=search_query)
            | Q(listing__stock_name__icontains=search_query)
            | Q(listing__company_name__icontains=search_query)
            | Q(member__display_name__icontains=search_query)
            | Q(account__account_name__icontains=search_query)
        )
        active_queryset = active_queryset.filter(search_filter)
        closed_queryset = closed_queryset.filter(search_filter)

    active_trades = list(
        active_queryset.order_by(
            "-application_date",
            "listing__stock_code",
            "member__display_name",
        )
    )
    closed_trades = list(
        closed_queryset.order_by(
            F("sell_date").desc(nulls_last=True),
            "-updated_at",
            "listing__stock_code",
            "member__display_name",
        )
    )
    applying_trades = [
        trade
        for trade in active_trades
        if trade.trade_status == HkIpoSubscriptionTrade.STATUS_APPLYING
    ]
    holding_trades = [
        trade
        for trade in active_trades
        if trade.trade_status == HkIpoSubscriptionTrade.STATUS_HOLDING
    ]
    application_record_count = metric_queryset.count()
    allotted_record_count = metric_queryset.filter(allotted_lots__gt=0).count()
    allotment_rate = (
        Decimal(allotted_record_count) / Decimal(application_record_count) * Decimal("100")
        if application_record_count
        else Decimal("0")
    )
    realized_profit_total = (
        profit_queryset.aggregate(total=Sum("realized_profit"))["total"]
        or Decimal("0")
    )
    stock_option_filter = (
        Q(subscription_trades__trade_status__in=HkIpoSubscriptionTrade.TERMINAL_STATUSES)
        | Q(subscription_trades__sold_lots__gt=0)
    )
    if selected_year != "all":
        stock_option_filter &= (
            Q(subscription_trades__sell_date__year=year)
            | Q(
                subscription_trades__sell_date__isnull=True,
                subscription_trades__listing__subscription_end_date__year=year,
            )
        )
    stock_options = (
        HkIpoListing.objects.filter(stock_option_filter)
        .annotate(
            latest_sell_date=Max(
                "subscription_trades__sell_date",
                filter=stock_option_filter,
            )
        )
        .distinct()
        .order_by(F("latest_sell_date").desc(nulls_last=True), "stock_name", "stock_code")
    )
    selected_stock_id = request.GET.get("stock", "").strip()
    stock_profit_total = None
    selected_stock = None
    if selected_stock_id.isdigit():
        selected_stock = stock_options.filter(pk=int(selected_stock_id)).first()
        if selected_stock:
            stock_profit_total = (
                profit_queryset.filter(listing=selected_stock)
                .aggregate(total=Sum("realized_profit"))["total"]
                or Decimal("0")
            )

    account_options = (
        BankAccount.objects.filter(
            pk__in=profit_queryset.exclude(account__isnull=True).values("account_id")
        )
        .select_related("member")
        .order_by("member__display_name", "account_name")
    )
    selected_account_id = request.GET.get("account", "").strip()
    account_profit_total = None
    selected_account = None
    if selected_account_id.isdigit():
        selected_account = account_options.filter(pk=int(selected_account_id)).first()
        if selected_account:
            account_profit_total = (
                profit_queryset.filter(account=selected_account)
                .aggregate(total=Sum("realized_profit"))["total"]
                or Decimal("0")
            )

    date_start = request.GET.get("date_start", "").strip()
    date_end = request.GET.get("date_end", "").strip()
    period_start_date = None
    period_end_date = None
    period_profit_total = None
    period_query_error = ""
    if date_start or date_end:
        period_trades = HkIpoSubscriptionTrade.objects.filter(sell_date__isnull=False)
        try:
            if date_start:
                period_start_date = date.fromisoformat(date_start)
                period_trades = period_trades.filter(sell_date__gte=period_start_date)
            if date_end:
                period_end_date = date.fromisoformat(date_end)
                period_trades = period_trades.filter(sell_date__lte=period_end_date)
            if period_start_date and period_end_date and period_start_date > period_end_date:
                raise ValueError
            period_profit_total = (
                period_trades.aggregate(total=Sum("realized_profit"))["total"]
                or Decimal("0")
            )
        except ValueError:
            period_query_error = "请选择有效的日期区间。"
            period_start_date = None
            period_end_date = None
    filtered_closed_trades = closed_trades
    if selected_stock:
        filtered_closed_trades = [trade for trade in filtered_closed_trades if trade.listing_id == selected_stock.pk]
    if selected_account:
        filtered_closed_trades = [
            trade
            for trade in filtered_closed_trades
            if trade.account_id == selected_account.pk
        ]
    if not period_query_error:
        if period_start_date:
            filtered_closed_trades = [trade for trade in filtered_closed_trades if trade.sell_date and trade.sell_date >= period_start_date]
        if period_end_date:
            filtered_closed_trades = [trade for trade in filtered_closed_trades if trade.sell_date and trade.sell_date <= period_end_date]
    ipo_trade_map = {trade.pk: trade for trade in filtered_closed_trades}
    closed_rows = []
    for transaction in ipo_sale_transactions(ipo_trade_map):
        ipo_trade = ipo_trade_map.get(transaction.extra_data.get("ipo_subscription_trade_id"))
        if not ipo_trade:
            continue
        transaction.ipo_trade = ipo_trade
        transaction.display_sold_lots = (
            transaction.quantity / (ipo_trade.listing.lot_size or 1)
            if transaction.quantity
            else 0
        )
        transaction.display_status_label = (
            "部分卖出"
            if ipo_trade.trade_status == HkIpoSubscriptionTrade.STATUS_HOLDING
            else "清仓"
        )
        transaction.display_status_class = (
            "partial"
            if ipo_trade.trade_status == HkIpoSubscriptionTrade.STATUS_HOLDING
            else "closed"
        )
        closed_rows.append(transaction)
    transaction_trade_ids = {
        row.ipo_trade.pk for row in closed_rows if getattr(row, "ipo_trade", None)
    }
    for trade in filtered_closed_trades:
        is_unallotted = trade.trade_status == HkIpoSubscriptionTrade.STATUS_UNALLOTTED
        if trade.pk in transaction_trade_ids or (not is_unallotted and not (trade.sold_lots or 0)):
            continue
        trade.ipo_trade = trade
        trade.display_sold_lots = trade.sold_lots
        trade.price = trade.sell_price
        trade.trade_date = trade.sell_date or trade.listing.allotment_result_date
        trade.fee = trade.trading_fee
        trade.realized_pnl = trade.realized_profit
        trade.legacy_sale_row = True
        trade.display_status_label = (
            "未中签"
            if is_unallotted
            else (
                "部分卖出"
                if trade.trade_status == HkIpoSubscriptionTrade.STATUS_HOLDING
                else "清仓"
            )
        )
        trade.display_status_class = (
            "unallotted"
            if is_unallotted
            else (
                "partial"
                if trade.trade_status == HkIpoSubscriptionTrade.STATUS_HOLDING
                else "closed"
            )
        )
        closed_rows.append(trade)
    closed_rows.sort(
        key=lambda row: (
            row.trade_date or date.min,
            getattr(row, "updated_at", None) or getattr(row, "created_at", None),
            row.pk,
        ),
        reverse=True,
    )
    closed_visible = closed_rows[:10]
    closed_hidden = closed_rows[10:]
    return render(
        request,
        "ipo/subscription_trade_list.html",
        {
            "metrics": {
                "applied": metric_queryset.values("listing_id").distinct().count(),
                "allotted": metric_queryset.filter(allotted_lots__gt=0)
                .values("listing_id")
                .distinct()
                .count(),
                "allotment_rate": allotment_rate,
                "holding": metric_queryset.filter(
                    trade_status=HkIpoSubscriptionTrade.STATUS_HOLDING
                )
                .values("listing_id")
                .distinct()
                .count(),
                "closed": metric_queryset.filter(
                    Q(trade_status=HkIpoSubscriptionTrade.STATUS_CLOSED)
                    | Q(sold_lots__gt=0),
                    allotted_lots__gt=0,
                )
                .values("listing_id")
                .distinct()
                .count(),
                "realized_profit_total": realized_profit_total,
            },
            "year_filter": {
                "available_years": available_years,
                "selected_year": selected_year,
            },
            "list_filters": {
                "q": search_query,
                "status": selected_status,
                "status_choices": HkIpoSubscriptionTrade.STATUS_CHOICES,
            },
            "applying_trades": applying_trades,
            "holding_trades": decorate_ipo_rows(holding_trades, holding=True),
            "closed_visible": closed_visible,
            "closed_hidden": closed_hidden,
            "closed_count": len(closed_rows),
            "profit_queries": {
                "stock_options": stock_options,
                "selected_stock": selected_stock,
                "selected_stock_id": selected_stock_id,
                "stock_profit_total": stock_profit_total,
                "account_options": account_options,
                "selected_account": selected_account,
                "selected_account_id": selected_account_id,
                "account_profit_total": account_profit_total,
                "date_start": date_start,
                "date_end": date_end,
                "period_profit_total": period_profit_total,
                "period_query_error": period_query_error,
            },
        },
    )


@login_required
def subscription_trade_create(request):
    if request.method == "POST":
        form = HkIpoSubscriptionTradeForm(request.POST)
        if form.is_valid():
            trade = stamp_actor(form.save(commit=False), request.user)
            trade.save()
            return redirect(subscription_trade_list_url(trade))
    else:
        form = HkIpoSubscriptionTradeForm()
    return render(
        request,
        "ipo/subscription_trade_form.html",
        {"form": form, "title": "编辑申购情况", "account_member_map": get_ipo_account_member_map()},
    )


@login_required
def subscription_trade_edit(request, pk):
    trade = get_object_or_404(HkIpoSubscriptionTrade, pk=pk)
    if request.method == "POST":
        form = HkIpoSubscriptionTradeForm(request.POST, instance=trade)
        if form.is_valid():
            trade = stamp_actor(form.save(commit=False), request.user)
            trade.save()
            return redirect(subscription_trade_list_url(trade))
    else:
        form = HkIpoSubscriptionTradeForm(instance=trade)
    return render(
        request,
        "ipo/subscription_trade_form.html",
        {"form": form, "title": "编辑申购情况", "account_member_map": get_ipo_account_member_map()},
    )



@login_required
def subscription_trade_detail(request, pk):
    trade = get_object_or_404(
        HkIpoSubscriptionTrade.objects.select_related("listing", "member", "account"),
        pk=pk,
    )
    sale_transactions = ipo_sale_transactions([trade.pk])
    trade.display_remaining_lots = max((trade.allotted_lots or 0) - (trade.sold_lots or 0), 0)
    for transaction in sale_transactions:
        transaction.display_sold_lots = (
            transaction.quantity / (trade.listing.lot_size or 1)
            if transaction.quantity
            else 0
        )
    return render(
        request,
        "ipo/subscription_trade_detail.html",
        {"trade": trade, "sale_transactions": sale_transactions},
    )


@login_required
def subscription_trade_allotment(request, pk):
    trade = get_object_or_404(HkIpoSubscriptionTrade, pk=pk)
    if request.method == "POST":
        form = HkIpoAllotmentForm(request.POST, instance=trade)
        if form.is_valid():
            trade = stamp_actor(form.save(commit=False), request.user)
            trade.save()
            return redirect(subscription_trade_list_url(trade))
    else:
        form = HkIpoAllotmentForm(instance=trade)
    return render(
        request,
        "ipo/subscription_trade_form.html",
        {"form": form, "title": "编辑中签情况", "account_member_map": get_ipo_account_member_map()},
    )


@login_required
def subscription_trade_sale(request, pk, transaction_id=None):
    trade = get_object_or_404(HkIpoSubscriptionTrade, pk=pk)
    transaction = None
    initial = {}
    if transaction_id:
        transaction = get_object_or_404(
            InvestmentTransaction,
            pk=transaction_id,
            source=TransactionSourceChoices.IMPORT,
            trade_type=TradeTypeChoices.SELL,
            extra_data__ipo_subscription_trade_id=trade.pk,
        )
        initial = {
            "sell_price": transaction.price,
            "sell_date": transaction.trade_date,
            "sold_lots": transaction.quantity / (trade.listing.lot_size or 1),
            "trading_fee": transaction.fee,
        }
    if request.method == "POST":
        form = HkIpoSaleForm(request.POST, ipo_trade=trade, initial=initial)
        if form.is_valid():
            save_ipo_sale_transaction(trade, form, transaction, request.user)
            return redirect(subscription_trade_list_url(trade))
    else:
        form = HkIpoSaleForm(ipo_trade=trade, initial=initial)
    return render(
        request,
        "ipo/subscription_trade_form.html",
        {"form": form, "title": "编辑卖出情况", "account_member_map": get_ipo_account_member_map()},
    )


@login_required
@require_POST
@transaction.atomic
def subscription_trade_sale_cancel(request, pk, transaction_id):
    trade = get_object_or_404(HkIpoSubscriptionTrade, pk=pk)
    sale = get_object_or_404(
        InvestmentTransaction,
        pk=transaction_id,
        source=TransactionSourceChoices.IMPORT,
        trade_type=TradeTypeChoices.SELL,
        extra_data__ipo_subscription_trade_id=trade.pk,
    )
    account = sale.account
    security = sale.security
    sale.delete()
    rebuild_position(account, security)
    refresh_ipo_sale_summary(trade)
    messages.success(request, "卖出记录已撤销，持仓状态已重新计算。")
    next_url = request.POST.get("next") or ""
    if url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
        return redirect(next_url)
    return redirect(subscription_trade_list_url(trade))

def subscription_trade_list_url(trade):
    relevant_date = (
        trade.sell_date
        if trade.trade_status in HkIpoSubscriptionTrade.TERMINAL_STATUSES
        else trade.listing.subscription_end_date or trade.application_date
    )
    base_url = reverse("ipo:subscription_trade_list")
    return f"{base_url}?year={relevant_date.year}" if relevant_date else base_url


@login_required
def subscription_trade_delete(request, pk):
    trade = get_object_or_404(HkIpoSubscriptionTrade, pk=pk)
    if request.method == "POST":
        trade.delete()
        messages.success(request, "申购记录已删除。")
        next_url = request.POST.get("next") or ""
        if url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
            return redirect(next_url)
        return redirect("ipo:subscription_trade_list")
    return redirect("ipo:subscription_trade_edit", pk=trade.pk)


def get_ipo_account_member_map():
    return {
        str(account.id): account.member_id
        for account in BankAccount.objects.filter(remark__icontains="打新账户", is_active=True)
    }


@login_required
def placeholder_submodule(request, title):
    return render(request, "placeholder.html", {"title": title, "message": f"{title} 子模块已预留，后续会继续完善。"})


@login_required
def allotment_index(request):
    return placeholder_submodule(request, "分配查询")


@login_required
def strategy_index(request):
    return placeholder_submodule(request, "买卖策略分析")


@login_required
def review_index(request):
    return placeholder_submodule(request, "交易复盘")


@login_required
def recognize_listing_image(request):
    max_image_size = 8 * 1024 * 1024
    allowed_image_types = {"image/jpeg", "image/png", "image/webp"}
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "只支持 POST 请求。"}, status=405)
    image = request.FILES.get("image")
    if not image:
        return JsonResponse({"ok": False, "error": "请先选择一张图片。"}, status=400)
    if (image.content_type or "").lower() not in allowed_image_types:
        return JsonResponse({"ok": False, "error": "仅支持 JPG、PNG 或 WebP 图片。"}, status=400)
    if image.size > max_image_size:
        return JsonResponse({"ok": False, "error": "图片不能超过 8 MB。"}, status=400)
    try:
        fields = recognize_ipo_listing_from_image(
            image,
            provider_id=request.POST.get("provider"),
        )
    except IpoImageRecognitionError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)
    return JsonResponse({"ok": True, "fields": fields})
