"""Read-oriented administration for option wheel decision evidence."""

from django.contrib import admin

from .models import (
    WheelBrokerAccountSnapshot,
    WheelCandidate,
    WheelDecision,
    WheelMarketSnapshot,
    WheelOptionQuoteSnapshot,
    WheelPolicy,
)


class EvidenceReadOnlyAdminMixin:
    actions = None

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_view_permission(self, request, obj=None):
        return bool(request.user and request.user.is_superuser)

    def has_module_permission(self, request):
        return bool(request.user and request.user.is_superuser)

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        if request.user and request.user.is_superuser:
            return queryset
        return queryset.none()


@admin.register(WheelPolicy)
class WheelPolicyAdmin(admin.ModelAdmin):
    list_display = (
        "family",
        "account",
        "underlying",
        "enabled",
        "preferred_dte_min",
        "preferred_dte_max",
        "preferred_premium_min",
        "preferred_premium_max",
        "max_underlying_nav_ratio",
        "ruleset_version",
        "updated_at",
    )
    list_filter = (
        "family",
        "account",
        "underlying",
        "enabled",
        "ruleset_version",
    )
    search_fields = (
        "family__name",
        "account__bank_account__account_name",
        "underlying__symbol",
    )
    autocomplete_fields = ("family", "account", "underlying")

    def has_delete_permission(self, request, obj=None):
        return False

    def has_add_permission(self, request):
        return bool(request.user and request.user.is_superuser)

    def has_change_permission(self, request, obj=None):
        return bool(request.user and request.user.is_superuser)

    def has_view_permission(self, request, obj=None):
        return bool(request.user and request.user.is_superuser)

    def has_module_permission(self, request):
        return bool(request.user and request.user.is_superuser)


@admin.register(WheelBrokerAccountSnapshot)
class WheelBrokerAccountSnapshotAdmin(
    EvidenceReadOnlyAdminMixin,
    admin.ModelAdmin,
):
    list_display = (
        "family",
        "account",
        "source_kind",
        "currency",
        "nav",
        "data_status",
        "source_as_of",
        "fetched_at",
    )
    list_filter = ("family", "account", "source_kind", "data_status")
    search_fields = (
        "family__name",
        "account__bank_account__account_name",
    )
    date_hierarchy = "source_as_of"
    list_select_related = ("family", "account")
    autocomplete_fields = ("family", "account")


@admin.register(WheelMarketSnapshot)
class WheelMarketSnapshotAdmin(
    EvidenceReadOnlyAdminMixin,
    admin.ModelAdmin,
):
    list_display = (
        "underlying",
        "provider",
        "last_price",
        "delay_status",
        "freshness_status",
        "data_quality",
        "source_as_of",
        "fetched_at",
    )
    list_filter = (
        "underlying",
        "provider",
        "delay_status",
        "freshness_status",
        "data_quality",
    )
    search_fields = ("underlying__symbol", "provider_symbol")
    date_hierarchy = "source_as_of"
    list_select_related = ("underlying",)
    autocomplete_fields = ("underlying",)


@admin.register(WheelOptionQuoteSnapshot)
class WheelOptionQuoteSnapshotAdmin(
    EvidenceReadOnlyAdminMixin,
    admin.ModelAdmin,
):
    list_display = (
        "underlying",
        "provider",
        "option_type",
        "expiration",
        "strike",
        "bid",
        "ask",
        "last",
        "implied_volatility",
        "data_quality",
        "quote_as_of",
    )
    list_filter = (
        "underlying",
        "provider",
        "option_type",
        "standard_status",
        "delay_status",
        "freshness_status",
        "data_quality",
    )
    search_fields = (
        "underlying__symbol",
        "provider_contract_code",
        "provider",
    )
    date_hierarchy = "quote_as_of"
    list_select_related = ("underlying", "market_snapshot")
    autocomplete_fields = ("underlying",)


@admin.register(WheelDecision)
class WheelDecisionAdmin(
    EvidenceReadOnlyAdminMixin,
    admin.ModelAdmin,
):
    list_display = (
        "family",
        "account",
        "underlying",
        "policy",
        "decision_time",
        "event_status",
        "technical_status",
        "overall_status",
        "ruleset_version",
    )
    list_filter = (
        "family",
        "account",
        "underlying",
        "event_status",
        "technical_status",
        "overall_status",
        "ruleset_version",
    )
    search_fields = (
        "family__name",
        "account__bank_account__account_name",
        "underlying__symbol",
    )
    date_hierarchy = "decision_time"
    list_select_related = (
        "family",
        "account",
        "underlying",
        "policy",
        "account_snapshot",
        "market_snapshot",
    )
    autocomplete_fields = (
        "family",
        "account",
        "underlying",
        "policy",
    )


@admin.register(WheelCandidate)
class WheelCandidateAdmin(
    EvidenceReadOnlyAdminMixin,
    admin.ModelAdmin,
):
    list_display = (
        "decision",
        "strategy",
        "status",
        "candidate_key",
        "contract_count",
        "premium_total",
        "break_even",
    )
    list_filter = ("strategy", "status")
    search_fields = (
        "candidate_key",
        "decision__underlying__symbol",
    )
    date_hierarchy = "created_at"
    list_select_related = ("decision", "option_quote")
    autocomplete_fields = ("decision",)
