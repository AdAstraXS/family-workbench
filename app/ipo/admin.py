from django.contrib import admin

from .models import HkIpoListing, HkIpoSubscriptionTrade


@admin.register(HkIpoListing)
class HkIpoListingAdmin(admin.ModelAdmin):
    list_display = (
        "stock_code",
        "stock_name",
        "company_name",
        "subscription_status",
        "listing_type",
        "mechanism",
        "subscription_end_date",
        "listing_date",
        "final_price",
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
    search_fields = ("stock_code", "stock_name", "company_name", "sector", "sponsor")
    readonly_fields = (
        "entry_fee",
        "public_offer_lots",
        "fundraising_amount_100m",
        "hk_connect_required_gain_pct",
        "created_at",
        "updated_at",
    )
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
