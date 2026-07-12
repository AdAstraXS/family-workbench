from django.contrib import admin

from .models import (
    BondDetail,
    DailyExchangeRateFetch,
    InvestmentAccount,
    InvestmentCashMovement,
    InvestmentPosition,
    InvestmentTransaction,
    InvestmentOption,
    OptionContract,
    PortfolioSnapshot,
    PortfolioSnapshotPositionLine,
    Security,
    SecurityMarketSnapshot,
    SecurityNews,
    WatchlistItem,
)
from .services import rebuild_cash_only_transaction, rebuild_position


@admin.register(InvestmentAccount)
class InvestmentAccountAdmin(admin.ModelAdmin):
    list_display = (
        "account_name",
        "member",
        "account_no_masked",
        "account_region",
        "is_active",
    )
    list_filter = ("bank_account__family", "bank_account__member", "bank_account__account_region", "bank_account__is_active")
    search_fields = ("bank_account__account_name", "bank_account__account_no_masked", "bank_account__remark")
    readonly_fields = ("bank_account",)

    def has_add_permission(self, request):
        return False


@admin.register(Security)
class SecurityAdmin(admin.ModelAdmin):
    list_display = ("symbol", "name", "market", "exchange", "asset_type", "currency", "lot_size", "data_source", "is_active")
    list_filter = ("market", "exchange", "asset_type", "currency", "data_source", "is_active")
    search_fields = ("symbol", "name", "industry")


@admin.register(OptionContract)
class OptionContractAdmin(admin.ModelAdmin):
    list_display = ("security", "underlying", "option_type", "strike_price", "expiration_date", "multiplier")
    list_filter = ("option_type", "expiration_date")
    search_fields = ("security__symbol", "underlying__symbol", "underlying__name")


@admin.register(BondDetail)
class BondDetailAdmin(admin.ModelAdmin):
    list_display = (
        "security", "issuer", "bond_type", "coupon_rate", "maturity_date",
        "quote_basis", "accrued_interest", "valuation_date",
    )
    list_filter = ("bond_type", "quote_basis", "maturity_date")
    search_fields = ("security__symbol", "security__name", "isin", "issuer")


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
    list_filter = ("account__bank_account__family", "account__bank_account__member", "security__market", "position_date")
    search_fields = ("account__bank_account__account_name", "security__symbol", "security__name", "remark")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(InvestmentTransaction)
class InvestmentTransactionAdmin(admin.ModelAdmin):
    list_display = ("transaction_no", "trade_date", "account", "security", "trade_type_option", "quantity", "price", "amount", "realized_pnl", "currency")
    list_filter = ("account__bank_account__family", "account__bank_account__member", "trade_type_option", "currency", "trade_date")
    search_fields = ("transaction_no", "account__bank_account__account_name", "security__symbol", "security__name", "remark")

    @staticmethod
    def _rebuild(account_id, security_id):
        item = InvestmentTransaction.objects.filter(
            account_id=account_id,
            security_id=security_id,
        ).select_related("account", "security").first()
        if item:
            rebuild_position(item.account, item.security)
            return
        from .models import InvestmentAccount, Security

        rebuild_position(
            InvestmentAccount.objects.get(pk=account_id),
            Security.objects.get(pk=security_id),
        )

    def save_model(self, request, obj, form, change):
        old_pair = None
        if change:
            old = InvestmentTransaction.objects.filter(pk=obj.pk).first()
            if old and old.security_id:
                old_pair = (old.account_id, old.security_id)
        super().save_model(request, obj, form, change)
        if obj.security_id:
            self._rebuild(obj.account_id, obj.security_id)
        else:
            rebuild_cash_only_transaction(obj)
        if old_pair and old_pair != (obj.account_id, obj.security_id):
            self._rebuild(*old_pair)

    def delete_model(self, request, obj):
        pair = (obj.account_id, obj.security_id) if obj.security_id else None
        super().delete_model(request, obj)
        if pair:
            self._rebuild(*pair)

    def delete_queryset(self, request, queryset):
        pairs = set(queryset.exclude(security=None).values_list("account_id", "security_id"))
        super().delete_queryset(request, queryset)
        for pair in pairs:
            self._rebuild(*pair)


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
    list_filter = ("account__bank_account__family", "account__bank_account__member", "movement_type", "currency", "source", "movement_date")
    search_fields = ("account__bank_account__account_name", "counterparty_account__account_name", "transaction__security__symbol", "external_id", "remark")


@admin.register(PortfolioSnapshot)
class PortfolioSnapshotAdmin(admin.ModelAdmin):
    list_display = ("snapshot_date", "family", "member", "account", "total_asset", "total_pnl", "currency")
    list_filter = ("family", "member", "currency", "snapshot_date")


@admin.register(PortfolioSnapshotPositionLine)
class PortfolioSnapshotPositionLineAdmin(admin.ModelAdmin):
    list_display = ("snapshot", "account", "asset_type", "asset_name", "quantity", "price", "market_value")
    list_filter = ("asset_type", "currency", "snapshot__snapshot_date")
    search_fields = ("asset_name", "security__symbol", "account__bank_account__account_name")


@admin.register(SecurityNews)
class SecurityNewsAdmin(admin.ModelAdmin):
    list_display = ("title", "security", "source", "sentiment", "published_at", "created_at")
    list_filter = ("source", "sentiment", "published_at")
    search_fields = ("title", "summary", "security__symbol", "security__name")
