from django.contrib import admin

from .forms import HkIpoListingForm
from .models import HkIpoListing, HkIpoListingOption, HkIpoSubscriptionTrade
from .services import fetch_hk_connect_threshold_100m


@admin.register(HkIpoListingOption)
class HkIpoListingOptionAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "category", "sort_order", "is_active", "updated_at")
    list_filter = ("category", "is_active")
    list_editable = ("sort_order", "is_active")
    search_fields = ("name", "code")
    ordering = ("category", "sort_order", "id")


@admin.register(HkIpoListing)
class HkIpoListingAdmin(admin.ModelAdmin):
    form = HkIpoListingForm
    list_display = (
        "stock_code",
        "stock_name",
        "company_name",
        "subscription_status",
        "listing_type_name",
        "mechanism_name",
        "subscription_end_date",
        "listing_date",
        "industry",
        "over_subscription_multiple",
        "final_price",
        "first_day_open_change_pct",
        "first_day_close_change_pct",
        "cumulative_change_pct",
        "entry_fee",
        "subscription_recommendation",
    )
    list_filter = (
        "listing_type",
        "mechanism",
        "subscription_recommendation",
        "valuation_comment",
        "subscription_status",
        "subscription_end_date",
        "listing_date",
    )
    search_fields = (
        "stock_code",
        "stock_name",
        "company_name",
        "sector",
        "industry",
        "sponsor",
    )
    readonly_fields = (
        "industry",
        "over_subscription_multiple",
        "first_day_open_change_pct",
        "first_day_close_change_pct",
        "cumulative_change_pct",
        "market_data_fetched_at",
        "entry_fee",
        "public_offer_lots",
        "fundraising_amount_100m",
        "hk_connect_threshold_100m",
        "hk_connect_required_gain_pct",
        "hk_connect_expectation_display",
        "created_at",
        "updated_at",
    )

    @admin.display(description="类型", ordering="listing_type")
    def listing_type_name(self, obj):
        return obj.get_listing_type_display()

    @admin.display(description="机制", ordering="mechanism")
    def mechanism_name(self, obj):
        return obj.get_mechanism_display()

    @admin.display(description="港股通预期")
    def hk_connect_expectation_display(self, obj):
        return obj.hk_connect_expectation

    def save_model(self, request, obj, form, change):
        threshold = fetch_hk_connect_threshold_100m()
        if threshold is not None:
            obj.hk_connect_threshold_100m = threshold
        super().save_model(request, obj, form, change)

    fieldsets = (
        ("基础资料", {
            "fields": (
                "stock_code",
                "stock_name",
                "company_name",
                "subscription_status",
                "listing_type",
                "mechanism",
                "sector",
                "business_summary",
                "prospectus",
            )
        }),
        ("发行排期", {
            "fields": (
                "subscription_start_date",
                "subscription_end_date",
                "allotment_result_date",
                "listing_date",
            )
        }),
        ("价格与规模", {
            "fields": (
                "offer_price_min",
                "offer_price_max",
                "final_price",
                "lot_size",
                "entry_fee",
                "public_offer_lots",
                "global_offer_shares_10k",
                "fundraising_amount_100m",
                "total_market_cap_100m",
                "h_share_market_cap_100m",
                "hk_connect_threshold_100m",
                "hk_connect_required_gain_pct",
                "hk_connect_expectation_display",
            )
        }),
        ("上市表现（利弗莫尔网页抓取）", {
            "fields": (
                "industry",
                "over_subscription_multiple",
                "first_day_open_change_pct",
                "first_day_close_change_pct",
                "cumulative_change_pct",
                "market_data_fetched_at",
            )
        }),
        ("发行结构", {
            "fields": (
                "sponsor",
                "has_sponsor_dealer",
                "has_greenshoe",
                "stabilizing_manager",
                "has_offer_size_adjustment",
                "offer_size_adjustment_pct",
                "has_cornerstone",
                "cornerstone_investors",
                "cornerstone_pct",
            )
        }),
        ("估值与决策", {
            "fields": (
                "pe_ratio",
                "ps_ratio",
                "comparable_companies",
                "valuation_comment",
                "fundamentals_score",
                "heat_score",
                "subscription_recommendation",
                "decision_reason",
                "remark",
            )
        }),
        ("系统字段", {
            "classes": ("collapse",),
            "fields": ("extra_data", "created_at", "updated_at"),
        }),
    )


@admin.register(HkIpoSubscriptionTrade)
class HkIpoSubscriptionTradeAdmin(admin.ModelAdmin):
    list_display = (
        "listing",
        "member",
        "account",
        "trade_status",
        "tranche",
        "applied_lots",
        "allotted_lots",
        "sold_lots",
        "sell_date",
        "realized_profit",
        "application_date",
    )
    list_filter = ("trade_status", "application_method", "tranche", "application_date", "sell_date")
    search_fields = ("listing__stock_code", "listing__stock_name", "member__display_name", "account__account_name", "remark")
    readonly_fields = (
        "application_date",
        "applied_shares",
        "application_amount",
        "financing_interest",
        "trade_status",
        "allotted_value",
        "allotment_fee",
        "realized_profit",
        "created_at",
        "updated_at",
    )
    fieldsets = (
        ("申购信息", {
            "fields": (
                "listing",
                "member",
                "account",
                "application_date",
                "tranche",
                "applied_lots",
                "applied_shares",
                "application_amount",
                "application_method",
                "financing_amount",
                "financing_rate",
                "financing_days",
                "financing_interest",
                "subscription_fee",
                "trade_status",
            )
        }),
        ("分配与交易", {
            "fields": (
                "allotted_lots",
                "allotted_value",
                "allotment_fee",
                "sell_price",
                "sell_date",
                "sold_lots",
                "trading_fee",
                "realized_profit",
            )
        }),
        ("备注", {"fields": ("remark", "extra_data", "created_at", "updated_at")}),
    )
