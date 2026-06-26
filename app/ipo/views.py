from datetime import date
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, F, Max, Q, Sum
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils import timezone

from .forms import HkIpoListingForm, HkIpoSubscriptionTradeForm
from .models import HkIpoListing, HkIpoSubscriptionTrade
from .services import (
    IpoImageRecognitionError,
    fetch_vbkr_expected_margin_multiples,
    recognize_ipo_listing_from_image,
)
from ledger.models import BankAccount


@login_required
def index(request):
    listing_counts = HkIpoListing.objects.values("subscription_status").annotate(total=Count("id"))
    listing_count_map = {item["subscription_status"]: item["total"] for item in listing_counts}
    trade_counts = HkIpoSubscriptionTrade.objects.values("trade_status").annotate(total=Count("listing", distinct=True))
    trade_count_map = {item["trade_status"]: item["total"] for item in trade_counts}
    realized_profit_total = (
        HkIpoSubscriptionTrade.objects.filter(trade_status=HkIpoSubscriptionTrade.STATUS_CLOSED)
        .aggregate(total=Sum("realized_profit"))["total"]
        or Decimal("0")
    )
    return render(
        request,
        "ipo/overview.html",
        {
            "metrics": {
                "subscribing": listing_count_map.get(HkIpoListing.STATUS_SUBSCRIBING, 0),
                "waiting_listing": listing_count_map.get(HkIpoListing.STATUS_WAITING_LISTING, 0),
                "listing_today": listing_count_map.get(HkIpoListing.STATUS_LISTING_TODAY, 0),
                "listed": listing_count_map.get(HkIpoListing.STATUS_LISTED, 0),
                "trade_applying": trade_count_map.get(HkIpoSubscriptionTrade.STATUS_APPLYING, 0),
                "trade_holding": trade_count_map.get(HkIpoSubscriptionTrade.STATUS_HOLDING, 0),
                "trade_closed": trade_count_map.get(HkIpoSubscriptionTrade.STATUS_CLOSED, 0),
                "realized_profit_total": realized_profit_total,
            }
        },
    )


@login_required
def listing_list(request):
    listings = list(HkIpoListing.objects.order_by("listing_date", "subscription_end_date", "stock_code", "stock_name"))
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

    expected_margin_map = fetch_vbkr_expected_margin_multiples()
    for listing in subscribing_listings:
        stock_code = (listing.stock_code or "").strip().upper()
        listing.expected_margin_multiple = (
            expected_margin_map.get(stock_code)
            or expected_margin_map.get(stock_code.replace(".HK", ""))
            or "-"
        )

    listed_visible = listed_listings[:10]
    listed_hidden = listed_listings[10:]
    metrics = {
        "subscribing": len(subscribing_listings),
        "waiting_listing": len(waiting_listings),
        "listing_today": len(today_listings),
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
        },
    )


@login_required
def listing_detail(request, pk):
    listing = get_object_or_404(HkIpoListing, pk=pk)
    return render(request, "ipo/listing_detail.html", {"listing": listing})


@login_required
def save_listing_form(request, title, instance=None):
    if request.method == "POST":
        form = HkIpoListingForm(request.POST, request.FILES, instance=instance)
        if form.is_valid():
            listing = form.save()
            return redirect("ipo:listing_detail", pk=listing.pk)
    else:
        form = HkIpoListingForm(instance=instance)
    return render(request, "ipo/listing_form.html", {"form": form, "title": title})


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
        selected_year = str(current_year) if current_year in available_years else "all"
    if selected_year != "all" and (
        not selected_year.isdigit() or int(selected_year) not in available_years
    ):
        selected_year = "all"
    search_query = request.GET.get("q", "").strip()
    selected_status = request.GET.get("status", "").strip()
    valid_statuses = {choice[0] for choice in HkIpoSubscriptionTrade.STATUS_CHOICES}
    if selected_status not in valid_statuses:
        selected_status = ""

    metric_queryset = HkIpoSubscriptionTrade.objects.all()
    profit_queryset = HkIpoSubscriptionTrade.objects.filter(
        trade_status=HkIpoSubscriptionTrade.STATUS_CLOSED
    )
    list_queryset = trade_queryset
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
        list_queryset = list_queryset.filter(listing__subscription_end_date__year=year)
    if selected_status:
        list_queryset = list_queryset.filter(trade_status=selected_status)
    if search_query:
        list_queryset = list_queryset.filter(
            Q(listing__stock_code__icontains=search_query)
            | Q(listing__stock_name__icontains=search_query)
            | Q(listing__company_name__icontains=search_query)
            | Q(member__display_name__icontains=search_query)
            | Q(account__account_name__icontains=search_query)
        )

    trades = list(
        list_queryset
        .order_by("-application_date", "listing__stock_code", "member__display_name")
    )
    applying_trades = [trade for trade in trades if trade.trade_status == HkIpoSubscriptionTrade.STATUS_APPLYING]
    holding_trades = [trade for trade in trades if trade.trade_status == HkIpoSubscriptionTrade.STATUS_HOLDING]
    closed_trades = [trade for trade in trades if trade.trade_status == HkIpoSubscriptionTrade.STATUS_CLOSED]
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
    stock_options = (
        HkIpoListing.objects.filter(subscription_trades__isnull=False)
        .annotate(latest_sell_date=Max("subscription_trades__sell_date"))
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
                HkIpoSubscriptionTrade.objects.filter(listing=selected_stock)
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
    if not period_query_error:
        if period_start_date:
            filtered_closed_trades = [trade for trade in filtered_closed_trades if trade.sell_date and trade.sell_date >= period_start_date]
        if period_end_date:
            filtered_closed_trades = [trade for trade in filtered_closed_trades if trade.sell_date and trade.sell_date <= period_end_date]
    closed_visible = filtered_closed_trades[:10]
    closed_hidden = filtered_closed_trades[10:]
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
                    trade_status=HkIpoSubscriptionTrade.STATUS_CLOSED
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
            "holding_trades": holding_trades,
            "closed_visible": closed_visible,
            "closed_hidden": closed_hidden,
            "closed_count": len(filtered_closed_trades),
            "profit_queries": {
                "stock_options": stock_options,
                "selected_stock": selected_stock,
                "selected_stock_id": selected_stock_id,
                "stock_profit_total": stock_profit_total,
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
            form.save()
            return redirect("ipo:subscription_trade_list")
    else:
        form = HkIpoSubscriptionTradeForm()
    return render(
        request,
        "ipo/subscription_trade_form.html",
        {"form": form, "title": "新增申购", "account_member_map": get_ipo_account_member_map()},
    )


@login_required
def subscription_trade_edit(request, pk):
    trade = get_object_or_404(HkIpoSubscriptionTrade, pk=pk)
    if request.method == "POST":
        form = HkIpoSubscriptionTradeForm(request.POST, instance=trade)
        if form.is_valid():
            form.save()
            return redirect("ipo:subscription_trade_list")
    else:
        form = HkIpoSubscriptionTradeForm(instance=trade)
    return render(
        request,
        "ipo/subscription_trade_form.html",
        {"form": form, "title": "编辑申购和交易", "account_member_map": get_ipo_account_member_map()},
    )


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
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "只支持 POST 请求。"}, status=405)
    image = request.FILES.get("image")
    if not image:
        return JsonResponse({"ok": False, "error": "请先选择一张图片。"}, status=400)
    if not (image.content_type or "").startswith("image/"):
        return JsonResponse({"ok": False, "error": "请上传图片文件。"}, status=400)
    try:
        fields = recognize_ipo_listing_from_image(image)
    except IpoImageRecognitionError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)
    return JsonResponse({"ok": True, "fields": fields})
