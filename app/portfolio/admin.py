from django import forms
from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.urls import reverse
from django.utils.html import format_html

from .forms import (
    clean_market_exchange,
    security_exchange_choices,
    security_market_choices,
    validate_security_market_selection,
)

from .models import (
    BondDetail,
    CashMovementTypeChoices,
    DailyPortfolioValuationRun,
    DailyExchangeRateFetch,
    InvestmentAccount,
    InvestmentCashMovement,
    InvestmentPosition,
    InvestmentTransaction,
    InvestmentOption,
    MarketDataRefreshRun,
    OptionContract,
    PortfolioAccountBalanceAnchor,
    PortfolioReconciliationLine,
    PortfolioReconciliationRun,
    PortfolioSnapshot,
    PortfolioSnapshotPositionLine,
    Security,
    SecurityExchange,
    SecurityMarket,
    SecurityMarketSnapshot,
    SecurityPriceRecord,
    SecurityQuoteConfig,
    SecurityNews,
    TransactionSourceChoices,
    WatchlistItem,
)
from .services import rebuild_cash_only_transaction, rebuild_position


class SecurityExchangeInline(admin.TabularInline):
    model = SecurityExchange
    extra = 0
    can_delete = False
    show_change_link = True
    readonly_fields = ("code",)
    fields = (
        "code", "name", "default_currency", "provider_prefix",
        "display_order", "is_active", "remark",
    )

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(SecurityMarket)
class SecurityMarketAdmin(admin.ModelAdmin):
    list_display = (
        "code", "name", "default_currency", "supports_futu",
        "display_order", "is_active",
    )
    list_editable = ("display_order", "is_active")
    list_filter = ("supports_futu", "is_active")
    search_fields = ("code", "name", "remark")
    inlines = (SecurityExchangeInline,)

    def has_delete_permission(self, request, obj=None):
        return False

    def get_readonly_fields(self, request, obj=None):
        return ("code",) if obj else ()


@admin.register(SecurityExchange)
class SecurityExchangeAdmin(admin.ModelAdmin):
    list_display = (
        "code", "name", "market", "default_currency", "provider_prefix",
        "display_order", "is_active",
    )
    list_editable = ("display_order", "is_active")
    list_filter = ("market", "is_active", "default_currency")
    search_fields = ("code", "name", "market__code", "market__name", "remark")

    def has_delete_permission(self, request, obj=None):
        return False

    def get_readonly_fields(self, request, obj=None):
        return ("market", "code") if obj else ()


class SecurityAdminForm(forms.ModelForm):
    class Meta:
        model = Security
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        current_market = self.instance.market if self.instance and self.instance.pk else ""
        current_exchange = self.instance.exchange if self.instance and self.instance.pk else ""
        self.fields["market"] = forms.ChoiceField(
            label="市场",
            choices=security_market_choices(current_market),
        )
        self.fields["exchange"] = forms.ChoiceField(
            label="交易所",
            required=False,
            choices=security_exchange_choices(current_market, current_exchange),
        )
        if current_exchange:
            self.initial["exchange"] = f"{current_market}:{current_exchange}"

    def clean(self):
        cleaned = super().clean()
        exchange = clean_market_exchange(self, cleaned)
        validate_security_market_selection(self, cleaned, exchange)
        return cleaned


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
    form = SecurityAdminForm
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
    list_display = (
        "security", "last_price", "price_source", "pricing_status",
        "price_as_of", "total_market_value", "pe_ttm_ratio", "pb_ratio",
        "fetched_at",
    )
    list_filter = ("price_source", "pricing_status", "is_delayed")
    search_fields = ("security__symbol", "security__name")


@admin.register(SecurityQuoteConfig)
class SecurityQuoteConfigAdmin(admin.ModelAdmin):
    list_display = (
        "security", "provider", "provider_symbol", "price_type",
        "max_age_hours", "enabled", "priority",
    )
    list_filter = ("provider", "enabled")
    search_fields = ("security__symbol", "security__name", "provider_symbol")


@admin.register(SecurityPriceRecord)
class SecurityPriceRecordAdmin(admin.ModelAdmin):
    list_display = (
        "security", "price", "currency", "source", "price_type",
        "price_as_of", "fetched_at",
    )
    list_filter = ("source", "price_type", "currency")
    search_fields = ("security__symbol", "security__name")
    readonly_fields = ("fetched_at",)


@admin.register(MarketDataRefreshRun)
class MarketDataRefreshRunAdmin(admin.ModelAdmin):
    list_display = (
        "started_at", "finished_at", "status", "scope", "target_count",
        "success_count", "stale_count", "missing_count", "error_count",
    )
    list_filter = ("status", "scope")
    readonly_fields = (
        "started_at", "finished_at", "status", "scope", "target_count",
        "success_count", "stale_count", "missing_count", "error_count",
        "details",
    )

    def has_add_permission(self, request):
        return False


@admin.register(DailyPortfolioValuationRun)
class DailyPortfolioValuationRunAdmin(admin.ModelAdmin):
    list_display = (
        "valuation_date",
        "family",
        "status",
        "started_at",
        "finished_at",
        "snapshot_count",
        "quote_success_count",
        "stale_price_count",
        "missing_price_count",
        "missing_exchange_rate_count",
        "error_count",
    )
    list_filter = ("status", "valuation_date", "exchange_rate_status")
    readonly_fields = (
        "family",
        "valuation_date",
        "started_at",
        "finished_at",
        "status",
        "market_refresh",
        "exchange_rate_status",
        "exchange_rate_source_date",
        "snapshot_count",
        "quote_success_count",
        "stale_price_count",
        "missing_price_count",
        "missing_exchange_rate_count",
        "error_count",
        "details",
    )

    def has_add_permission(self, request):
        return False


@admin.register(InvestmentPosition)
class InvestmentPositionAdmin(admin.ModelAdmin):
    list_display = (
        "account", "security", "quantity", "avg_cost", "diluted_cost",
        "current_price", "current_price_source", "pricing_status",
        "current_price_as_of", "market_value", "position_date",
    )
    list_filter = (
        "account__bank_account__family", "account__bank_account__member",
        "security__market", "current_price_source", "pricing_status",
        "position_date",
    )
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
    INDEPENDENT_TYPES = {
        CashMovementTypeChoices.DEPOSIT,
        CashMovementTypeChoices.WITHDRAWAL,
        CashMovementTypeChoices.EXCHANGE,
        CashMovementTypeChoices.TRANSFER,
        CashMovementTypeChoices.ADJUSTMENT,
    }
    LINKED_READONLY_FIELDS = (
        "account",
        "transaction",
        "counterparty_account",
        "movement_date",
        "settlement_date",
        "movement_type",
        "currency",
        "amount",
        "source",
        "external_id",
        "remark",
        "created_by",
        "updated_by",
    )
    list_display = (
        "movement_date",
        "account",
        "record_kind",
        "movement_type",
        "currency",
        "amount",
        "counterparty_account",
        "transaction_link",
        "source",
    )
    list_filter = (
        "account__bank_account__family",
        "account__bank_account__member",
        ("transaction", admin.EmptyFieldListFilter),
        "movement_type",
        "currency",
        "source",
        "movement_date",
    )
    search_fields = ("account__bank_account__account_name", "counterparty_account__account_name", "transaction__security__symbol", "external_id", "remark")

    def get_queryset(self, request):
        return super().get_queryset(request).select_related(
            "transaction", "transaction__security"
        )

    def formfield_for_choice_field(self, db_field, request, **kwargs):
        if db_field.name == "movement_type":
            kwargs["choices"] = [
                choice
                for choice in CashMovementTypeChoices.choices
                if choice[0] in self.INDEPENDENT_TYPES
            ]
        return super().formfield_for_choice_field(db_field, request, **kwargs)

    def get_readonly_fields(self, request, obj=None):
        if obj and (
            obj.transaction_id
            or obj.source == TransactionSourceChoices.RECONCILIATION
        ):
            return self.LINKED_READONLY_FIELDS
        return ("transaction", "source", "external_id")

    def has_change_permission(self, request, obj=None):
        if obj and (
            obj.transaction_id
            or obj.source == TransactionSourceChoices.RECONCILIATION
        ):
            return False
        return super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        if obj and (
            obj.transaction_id
            or obj.source == TransactionSourceChoices.RECONCILIATION
        ):
            return False
        return super().has_delete_permission(request, obj)

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)
        return actions

    def delete_model(self, request, obj):
        if obj.transaction_id:
            raise PermissionDenied("交易派生现金流水不能直接删除，请修改或删除关联交易。")
        if obj.source == TransactionSourceChoices.RECONCILIATION:
            raise PermissionDenied("账本差额对齐流水只能通过差额对齐页面撤销。")
        super().delete_model(request, obj)

    @admin.display(description="记录性质")
    def record_kind(self, obj):
        return "交易派生" if obj.transaction_id else "独立现金"

    @admin.display(description="关联交易")
    def transaction_link(self, obj):
        if not obj.transaction_id:
            return "—"
        url = reverse(
            "admin:portfolio_investmenttransaction_change",
            args=[obj.transaction_id],
        )
        label = obj.transaction.transaction_no or f"交易 #{obj.transaction_id}"
        return format_html('<a href="{}">{}</a>', url, label)


@admin.register(PortfolioAccountBalanceAnchor)
class PortfolioAccountBalanceAnchorAdmin(admin.ModelAdmin):
    list_display = (
        "anchor_date",
        "account",
        "currency",
        "original_amount",
        "recorded_base_amount",
        "reason",
        "carry_forward",
        "is_confirmed",
    )
    list_filter = (
        "anchor_date",
        "currency",
        "reason",
        "carry_forward",
        "is_confirmed",
    )
    search_fields = (
        "account__bank_account__account_name",
        "account__bank_account__member__display_name",
        "remark",
    )
    autocomplete_fields = ("account", "ledger_snapshot")


class PortfolioReconciliationLineInline(admin.TabularInline):
    model = PortfolioReconciliationLine
    extra = 0
    can_delete = False
    readonly_fields = (
        "account",
        "currency",
        "ledger_base_amount",
        "calculated_base_amount",
        "adjustment_base_amount",
        "adjustment_original_amount",
        "movement",
    )

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(PortfolioReconciliationRun)
class PortfolioReconciliationRunAdmin(admin.ModelAdmin):
    list_display = (
        "ledger_snapshot",
        "family",
        "status",
        "base_currency",
        "applied_by",
        "applied_at",
        "reverted_at",
    )
    list_filter = ("status", "ledger_snapshot__snapshot_date", "family")
    readonly_fields = (
        "family",
        "ledger_snapshot",
        "base_currency",
        "status",
        "applied_by",
        "applied_at",
        "reverted_by",
        "reverted_at",
        "report",
        "created_at",
        "updated_at",
    )
    inlines = (PortfolioReconciliationLineInline,)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


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
