from django.contrib import admin

from .models import (
    DailyExchangeRateFetch,
    InvestmentAccount,
    InvestmentCashMovement,
    InvestmentPosition,
    InvestmentTransaction,
    InvestmentOption,
    PortfolioSnapshot,
    Security,
    SecurityMarketSnapshot,
    SecurityNews,
    WatchlistItem,
)


@admin.register(InvestmentAccount)
class InvestmentAccountAdmin(admin.ModelAdmin):
    list_display = (
        "account_name",
        "member",
        "account_no_masked",
        "account_region",
        "is_active",
    )
    list_filter = ("family", "member", "account_region", "is_active")
    search_fields = ("account_name", "account_no_masked", "remark")
    readonly_fields = (
        "bank_account",
        "family",
        "member",
        "account_name",
        "account_no_masked",
        "account_region",
    )

    def has_add_permission(self, request):
        return False


@admin.register(Security)
class SecurityAdmin(admin.ModelAdmin):
    list_display = ("symbol", "name", "market", "exchange", "asset_type", "currency", "lot_size", "data_source", "is_active")
    list_filter = ("market", "exchange", "asset_type", "currency", "data_source", "is_active")
    search_fields = ("symbol", "name", "industry")


@admin.register(WatchlistItem)
class WatchlistItemAdmin(admin.ModelAdmin):
    list_display = ("security", "family", "member", "is_active", "created_at")
    list_filter = ("family", "member", "is_active")
    search_fields = ("security__symbol", "security__name", "remark")


@admin.register(SecurityMarketSnapshot)
class SecurityMarketSnapshotAdmin(admin.ModelAdmin):
    list_display = ("security", "last_price", "total_market_value", "pe_ttm_ratio", "pb_ratio", "quote_time", "fetched_at")
    search_fields = ("security__symbol", "security__name")


@admin.register(InvestmentPosition)
class InvestmentPositionAdmin(admin.ModelAdmin):
    list_display = ("account", "security", "quantity", "avg_cost", "diluted_cost", "current_price", "market_value", "position_date")
    list_filter = ("account__family", "account__member", "security__market", "position_date")
    search_fields = ("account__account_name", "security__symbol", "security__name", "remark")


@admin.register(InvestmentTransaction)
class InvestmentTransactionAdmin(admin.ModelAdmin):
    list_display = ("transaction_no", "trade_date", "account", "security", "trade_type_option", "quantity", "price", "amount", "realized_pnl", "currency")
    list_filter = ("account__family", "account__member", "trade_type_option", "currency", "trade_date")
    search_fields = ("transaction_no", "account__account_name", "security__symbol", "security__name", "remark")


@admin.register(InvestmentOption)
class InvestmentOptionAdmin(admin.ModelAdmin):
    list_display = ("category", "code", "name", "sort_order", "is_active")
    list_filter = ("category", "is_active")
    search_fields = ("code", "name")
    ordering = ("category", "sort_order", "pk")


@admin.register(DailyExchangeRateFetch)
class DailyExchangeRateFetchAdmin(admin.ModelAdmin):
    list_display = ("fetch_date", "source_date", "status", "fetched_at")
    list_filter = ("status", "fetch_date", "source_date")


@admin.register(InvestmentCashMovement)
class InvestmentCashMovementAdmin(admin.ModelAdmin):
    list_display = ("movement_date", "account", "movement_type", "currency", "amount", "counterparty_account", "transaction", "source")
    list_filter = ("account__family", "account__member", "movement_type", "currency", "source", "movement_date")
    search_fields = ("account__account_name", "counterparty_account__account_name", "transaction__security__symbol", "external_id", "remark")


@admin.register(PortfolioSnapshot)
class PortfolioSnapshotAdmin(admin.ModelAdmin):
    list_display = ("snapshot_date", "family", "member", "account", "total_asset", "total_pnl", "currency")
    list_filter = ("family", "member", "currency", "snapshot_date")


@admin.register(SecurityNews)
class SecurityNewsAdmin(admin.ModelAdmin):
    list_display = ("title", "security", "source", "sentiment", "published_at", "created_at")
    list_filter = ("source", "sentiment", "published_at")
    search_fields = ("title", "summary", "security__symbol", "security__name")
