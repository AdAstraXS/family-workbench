from django.contrib import admin

from .models import (
    InvestmentAccount,
    InvestmentPosition,
    InvestmentTransaction,
    PortfolioSnapshot,
    Security,
    SecurityNews,
)


@admin.register(InvestmentAccount)
class InvestmentAccountAdmin(admin.ModelAdmin):
    list_display = ("account_name", "member", "broker_name", "market_scope", "currency", "cash_balance", "is_active")
    list_filter = ("family", "member", "broker_name", "currency", "is_active")
    search_fields = ("account_name", "broker_name", "account_no_masked", "remark")


@admin.register(Security)
class SecurityAdmin(admin.ModelAdmin):
    list_display = ("symbol", "name", "market", "asset_type", "currency", "industry", "is_active")
    list_filter = ("market", "asset_type", "currency", "is_active")
    search_fields = ("symbol", "name", "industry")


@admin.register(InvestmentPosition)
class InvestmentPositionAdmin(admin.ModelAdmin):
    list_display = ("account", "security", "quantity", "avg_cost", "current_price", "market_value", "position_date")
    list_filter = ("account__family", "account__member", "security__market", "position_date")
    search_fields = ("account__account_name", "security__symbol", "security__name", "remark")


@admin.register(InvestmentTransaction)
class InvestmentTransactionAdmin(admin.ModelAdmin):
    list_display = ("trade_date", "account", "security", "trade_type", "quantity", "price", "amount", "currency")
    list_filter = ("account__family", "account__member", "trade_type", "currency", "trade_date")
    search_fields = ("account__account_name", "security__symbol", "security__name", "remark")


@admin.register(PortfolioSnapshot)
class PortfolioSnapshotAdmin(admin.ModelAdmin):
    list_display = ("snapshot_date", "family", "member", "account", "total_asset", "total_pnl", "currency")
    list_filter = ("family", "member", "currency", "snapshot_date")


@admin.register(SecurityNews)
class SecurityNewsAdmin(admin.ModelAdmin):
    list_display = ("title", "security", "source", "sentiment", "published_at", "created_at")
    list_filter = ("source", "sentiment", "published_at")
    search_fields = ("title", "summary", "security__symbol", "security__name")
