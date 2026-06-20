from django.contrib import admin

from .models import AccountRegion, AccountType, AssetCategory, Currency, ExchangeRate, Family, FamilyMember


@admin.register(Family)
class FamilyAdmin(admin.ModelAdmin):
    list_display = ("name", "base_currency", "created_at", "updated_at")
    search_fields = ("name", "remark")


@admin.register(FamilyMember)
class FamilyMemberAdmin(admin.ModelAdmin):
    list_display = ("display_name", "family", "role", "is_active", "created_at")
    list_filter = ("family", "role", "is_active")
    search_fields = ("display_name", "remark")


@admin.register(Currency)
class CurrencyAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "symbol", "is_active")
    list_filter = ("is_active",)
    search_fields = ("code", "name")


@admin.register(ExchangeRate)
class ExchangeRateAdmin(admin.ModelAdmin):
    list_display = ("base_currency", "quote_currency", "rate", "rate_date", "source")
    list_filter = ("base_currency", "quote_currency", "rate_date")
    search_fields = ("source",)


@admin.register(AccountType)
class AccountTypeAdmin(admin.ModelAdmin):
    list_display = ("name", "family", "display_order", "is_active")
    list_filter = ("family", "is_active")
    search_fields = ("name", "remark")


@admin.register(AssetCategory)
class AssetCategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "family", "display_order", "is_active")
    list_filter = ("family", "is_active")
    search_fields = ("name", "remark")


@admin.register(AccountRegion)
class AccountRegionAdmin(admin.ModelAdmin):
    list_display = ("name", "family", "display_order", "is_active")
    list_filter = ("family", "is_active")
    search_fields = ("name", "remark")
