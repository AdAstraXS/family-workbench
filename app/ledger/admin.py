from django.contrib import admin
from django import forms
from django.db.models import Prefetch
from django.utils.html import format_html_join
from django.utils import timezone

from .models import (
    AnnualBudget,
    AnnualBudgetLine,
    AssetBalanceEntry,
    AssetBalanceSnapshot,
    BankAccount,
    CashflowMonthlySummary,
    ExpenseCategory,
    ExpenseRecord,
    IncomeCategory,
    IncomeRecord,
)
from portfolio.models import VisibilityChoices


CURRENCY_CHOICES = [
    ("CNY", "人民币 CNY"),
    ("HKD", "港币 HKD"),
    ("USD", "美元 USD"),
]


def format_money(amount):
    return f"{amount or 0:,.0f}"


class AccountSelect(forms.Select):
    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        option = super().create_option(name, value, label, selected, index, subindex=subindex, attrs=attrs)
        if value and hasattr(value, "instance"):
            option["attrs"]["data-member-id"] = str(value.instance.member_id)
        return option


def calculate_base_amount(snapshot, currency, original_amount):
    currency = (currency or snapshot.base_currency or "CNY").upper()
    if currency == "USD":
        return original_amount * snapshot.usd_to_base
    if currency == "HKD":
        return original_amount * snapshot.hkd_to_base
    return original_amount


def prepare_asset_entry(entry, snapshot, display_order=None):
    entry.snapshot = snapshot
    entry.account_name = entry.account.account_name if entry.account else entry.account_name
    entry.base_amount = calculate_base_amount(snapshot, entry.currency, entry.original_amount)
    if display_order is not None:
        entry.display_order = display_order
    return entry


@admin.register(BankAccount)
class BankAccountAdmin(admin.ModelAdmin):
    list_display = ("account_name", "member", "account_type_ref", "account_region", "account_no_masked", "is_active")
    list_filter = ("family", "member", "account_type_ref", "account_region", "is_active")
    search_fields = ("account_name", "account_no_masked", "remark")


@admin.register(IncomeCategory)
class IncomeCategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "family", "parent", "is_active")
    list_filter = ("family", "is_active")
    search_fields = ("name",)


@admin.register(ExpenseCategory)
class ExpenseCategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "family", "parent", "is_active")
    list_filter = ("family", "is_active")
    search_fields = ("name",)


@admin.register(IncomeRecord)
class IncomeRecordAdmin(admin.ModelAdmin):
    fields = ("family", "member", "category", "period_start", "period_end", "amount", "currency", "is_recurring", "remark")
    list_display = ("period_start", "period_end", "member", "category", "amount", "currency", "is_recurring")
    list_filter = ("family", "member", "category", "currency", "period_start", "period_end", "is_recurring")
    search_fields = ("remark",)

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        if db_field.name == "currency":
            kwargs["widget"] = forms.Select(choices=CURRENCY_CHOICES)
        return super().formfield_for_dbfield(db_field, request, **kwargs)

    def save_model(self, request, obj, form, change):
        obj.income_date = obj.period_end or obj.period_start or timezone.localdate()
        obj.bank_account = None
        obj.source_name = ""
        obj.visibility = VisibilityChoices.PRIVATE
        super().save_model(request, obj, form, change)


@admin.register(ExpenseRecord)
class ExpenseRecordAdmin(admin.ModelAdmin):
    fields = ("family", "member", "category", "period_start", "period_end", "amount", "currency", "remark")
    list_display = ("period_start", "period_end", "member", "category", "amount", "currency")
    list_filter = ("family", "member", "category", "currency", "period_start", "period_end")
    search_fields = ("remark",)

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        if db_field.name == "currency":
            kwargs["widget"] = forms.Select(choices=CURRENCY_CHOICES)
        return super().formfield_for_dbfield(db_field, request, **kwargs)

    def save_model(self, request, obj, form, change):
        obj.expense_date = obj.period_end or obj.period_start or timezone.localdate()
        obj.bank_account = None
        obj.merchant = ""
        obj.payment_method = ""
        obj.visibility = VisibilityChoices.PRIVATE
        super().save_model(request, obj, form, change)


@admin.register(CashflowMonthlySummary)
class CashflowMonthlySummaryAdmin(admin.ModelAdmin):
    list_display = ("family", "member", "year", "month", "total_income", "total_expense", "net_cashflow", "currency")
    list_filter = ("family", "member", "year", "month", "currency")

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        if db_field.name == "currency":
            kwargs["widget"] = forms.Select(choices=CURRENCY_CHOICES)
        return super().formfield_for_dbfield(db_field, request, **kwargs)


class AnnualBudgetLineInline(admin.TabularInline):
    model = AnnualBudgetLine
    fields = ("line_type", "income_category", "expense_category", "annual_amount", "remark")
    extra = 4


@admin.register(AnnualBudget)
class AnnualBudgetAdmin(admin.ModelAdmin):
    list_display = ("year", "family", "updated_at")
    list_filter = ("family", "year")
    search_fields = ("remark",)
    inlines = [AnnualBudgetLineInline]


@admin.register(AnnualBudgetLine)
class AnnualBudgetLineAdmin(admin.ModelAdmin):
    fields = ("budget", "line_type", "income_category", "expense_category", "annual_amount", "remark")
    list_display = ("budget", "line_type", "income_category", "expense_category", "annual_amount")
    list_filter = ("budget", "line_type")
    search_fields = ("remark", "income_category__name", "expense_category__name")


class AssetBalanceEntryInline(admin.TabularInline):
    model = AssetBalanceEntry
    fields = ("member", "account", "asset_category", "currency", "original_amount", "remark")
    extra = 3

    class Media:
        css = {"all": ("css/admin_ledger.css",)}
        js = ("js/admin_asset_entry.js",)

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "account":
            kwargs["queryset"] = BankAccount.objects.filter(is_active=True).select_related("member").order_by("member__display_name", "account_name")
            kwargs["widget"] = AccountSelect()
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        if db_field.name == "currency":
            kwargs["widget"] = forms.Select(choices=CURRENCY_CHOICES)
        if db_field.name == "remark":
            kwargs["widget"] = forms.Textarea(attrs={"rows": 1, "style": "height: 32px;"})
        return super().formfield_for_dbfield(db_field, request, **kwargs)


@admin.register(AssetBalanceSnapshot)
class AssetBalanceSnapshotAdmin(admin.ModelAdmin):
    fields = ("family", "snapshot_date", "usd_to_base", "hkd_to_base", "remark")
    list_display = ("snapshot_date", "family", "member_balance_summary", "total_balance", "updated_at")
    list_filter = ("family", "snapshot_date")
    search_fields = ("remark",)
    inlines = [AssetBalanceEntryInline]

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .prefetch_related(Prefetch("entries", queryset=AssetBalanceEntry.objects.select_related("member")))
        )

    @admin.display(description="家庭成员余额")
    def member_balance_summary(self, obj):
        totals = {}
        for entry in obj.entries.all():
            if not entry.member_id:
                continue
            name = entry.member.display_name
            totals[name] = totals.get(name, 0) + (entry.base_amount or 0)
        if not totals:
            return "-"
        return format_html_join("", "{}: {}<br>", ((name, format_money(amount)) for name, amount in totals.items()))

    @admin.display(description="合计余额")
    def total_balance(self, obj):
        total = sum((entry.base_amount or 0 for entry in obj.entries.all()), 0)
        return format_money(total)

    def save_model(self, request, obj, form, change):
        obj.base_currency = "CNY"
        obj.title = ""
        super().save_model(request, obj, form, change)

    def save_formset(self, request, form, formset, change):
        instances = formset.save(commit=False)
        for deleted_object in formset.deleted_objects:
            deleted_object.delete()
        for index, instance in enumerate(instances, start=1):
            prepare_asset_entry(instance, form.instance, index)
            instance.save()
        formset.save_m2m()


@admin.register(AssetBalanceEntry)
class AssetBalanceEntryAdmin(admin.ModelAdmin):
    fields = ("snapshot", "member", "account", "asset_category", "currency", "original_amount", "remark")
    list_display = ("snapshot", "member", "account", "asset_category", "currency", "original_amount", "base_amount")
    list_filter = ("snapshot", "member", "currency", "asset_category")
    search_fields = ("account__account_name", "account_name", "asset_category__name", "remark")

    class Media:
        css = {"all": ("css/admin_ledger.css",)}
        js = ("js/admin_asset_entry.js",)

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "account":
            kwargs["queryset"] = BankAccount.objects.filter(is_active=True).select_related("member").order_by("member__display_name", "account_name")
            kwargs["widget"] = AccountSelect()
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        if db_field.name == "currency":
            kwargs["widget"] = forms.Select(choices=CURRENCY_CHOICES)
        if db_field.name == "remark":
            kwargs["widget"] = forms.Textarea(attrs={"rows": 1, "style": "height: 32px;"})
        return super().formfield_for_dbfield(db_field, request, **kwargs)

    def save_model(self, request, obj, form, change):
        if not obj.display_order:
            latest_order = (
                AssetBalanceEntry.objects.filter(snapshot=obj.snapshot)
                .exclude(pk=obj.pk)
                .order_by("-display_order")
                .values_list("display_order", flat=True)
                .first()
            )
            obj.display_order = (latest_order or 0) + 1
        prepare_asset_entry(obj, obj.snapshot)
        super().save_model(request, obj, form, change)
