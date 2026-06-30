from django.contrib import admin
from django import forms
from django.db.models import Prefetch
from django.utils.html import format_html_join
from django.utils import timezone
from datetime import datetime, time

from family_core.models import Family

from .models import (
    AnnualBudget,
    AnnualBudgetLine,
    AssetBalanceEntry,
    AssetBalanceSnapshot,
    BankAccount,
    CashflowMonthlySummary,
    ExpenseCategory,
    ExpenseImportBatch,
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


class CategoryPathChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return str(obj)


class CategoryParentSelect(forms.Select):
    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        option = super().create_option(
            name,
            value,
            label,
            selected,
            index,
            subindex=subindex,
            attrs=attrs,
        )
        if value and hasattr(value, "instance"):
            option["attrs"]["data-family-id"] = str(value.instance.family_id)
            option["attrs"]["data-parent-id"] = str(value.instance.parent_id or "")
        return option


class HierarchicalCategoryAdminForm(forms.ModelForm):
    LEVEL_CHOICES = (("1", "一级分类"), ("2", "二级分类"), ("3", "三级分类"))
    category_model = None

    category_level = forms.ChoiceField(label="分类层级", choices=LEVEL_CHOICES)
    primary_category = CategoryPathChoiceField(
        label="所属一级分类",
        queryset=IncomeCategory.objects.none(),
        required=False,
        widget=CategoryParentSelect,
        help_text="新增二级、三级分类时必选。",
    )
    secondary_category = CategoryPathChoiceField(
        label="所属二级分类",
        queryset=IncomeCategory.objects.none(),
        required=False,
        widget=CategoryParentSelect,
        help_text="仅新增三级分类时必选。",
    )

    class Meta:
        fields = (
            "family",
            "category_level",
            "primary_category",
            "secondary_category",
            "name",
            "is_active",
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        model = self.category_model
        self.fields["primary_category"].queryset = model.objects.none()
        if "secondary_category" in self.fields:
            self.fields["secondary_category"].queryset = model.objects.none()

        family_id = self.data.get(self.add_prefix("family")) if self.is_bound else None
        if not family_id and self.instance.pk:
            family_id = self.instance.family_id
        if not family_id and not self.is_bound:
            default_family = Family.objects.filter(name="我的家庭").first() or Family.objects.first()
            if default_family:
                family_id = default_family.pk
                self.initial["family"] = default_family.pk
        if family_id:
            primary_queryset = model.objects.filter(
                family_id=family_id,
                parent__isnull=True,
            ).order_by("name")
            secondary_queryset = model.objects.filter(
                family_id=family_id,
                parent__isnull=False,
                parent__parent__isnull=True,
            ).select_related("parent").order_by("parent__name", "name")
            if self.instance.pk:
                primary_queryset = primary_queryset.exclude(pk=self.instance.pk)
                secondary_queryset = secondary_queryset.exclude(pk=self.instance.pk)
            self.fields["primary_category"].queryset = primary_queryset
            if "secondary_category" in self.fields:
                self.fields["secondary_category"].queryset = secondary_queryset

        if self.instance.pk and not self.is_bound:
            path = []
            category = self.instance
            while category:
                path.append(category)
                category = category.parent
            path.reverse()
            self.fields["category_level"].initial = str(len(path))
            if len(path) >= 2:
                self.fields["primary_category"].initial = path[0]
            if len(path) >= 3 and "secondary_category" in self.fields:
                self.fields["secondary_category"].initial = path[1]
        elif not self.is_bound:
            self.fields["category_level"].initial = "1"

    def clean(self):
        cleaned_data = super().clean()
        family = cleaned_data.get("family")
        level = cleaned_data.get("category_level")
        primary = cleaned_data.get("primary_category")
        secondary = cleaned_data.get("secondary_category")
        name = (cleaned_data.get("name") or "").strip()

        if level == "1":
            parent = None
        elif level == "2":
            parent = primary
            if primary is None:
                self.add_error("primary_category", "二级分类必须选择所属一级分类。")
        elif level == "3":
            parent = secondary
            if primary is None:
                self.add_error("primary_category", "三级分类必须选择所属一级分类。")
            if secondary is None:
                self.add_error("secondary_category", "三级分类必须选择所属二级分类。")
            elif primary and secondary.parent_id != primary.pk:
                self.add_error("secondary_category", "所选二级分类不属于所选一级分类。")
        else:
            parent = None

        for field_name, category in (
            ("primary_category", primary),
            ("secondary_category", secondary),
        ):
            if category and family and category.family_id != family.pk:
                self.add_error(field_name, "所选分类必须属于同一家庭。")

        if family and name and level in {"1", "2", "3"} and not self._errors:
            duplicates = self.category_model.objects.filter(
                family=family,
                parent=parent,
                name=name,
            )
            if self.instance.pk:
                duplicates = duplicates.exclude(pk=self.instance.pk)
            if duplicates.exists():
                self.add_error("name", "同一层级下已存在同名分类。")
        cleaned_data["_resolved_parent"] = parent
        return cleaned_data

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.parent = self.cleaned_data.get("_resolved_parent")
        if commit:
            instance.save()
            self.save_m2m()
        return instance


class IncomeCategoryAdminForm(HierarchicalCategoryAdminForm):
    category_model = IncomeCategory

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields.pop("secondary_category", None)
        self.fields["category_level"].choices = (("1", "一级分类"), ("2", "二级分类"))

    class Meta(HierarchicalCategoryAdminForm.Meta):
        model = IncomeCategory
        fields = (
            "family",
            "category_level",
            "primary_category",
            "name",
            "is_active",
        )


class ExpenseCategoryAdminForm(HierarchicalCategoryAdminForm):
    category_model = ExpenseCategory

    primary_category = CategoryPathChoiceField(
        label="所属一级分类",
        queryset=ExpenseCategory.objects.none(),
        required=False,
        widget=CategoryParentSelect,
        help_text="新增二级、三级分类时必选。",
    )
    secondary_category = CategoryPathChoiceField(
        label="所属二级分类",
        queryset=ExpenseCategory.objects.none(),
        required=False,
        widget=CategoryParentSelect,
        help_text="仅新增三级分类时必选。",
    )

    class Meta(HierarchicalCategoryAdminForm.Meta):
        model = ExpenseCategory


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
    form = IncomeCategoryAdminForm
    fields = (
        "family",
        "category_level",
        "primary_category",
        "name",
        "is_active",
    )
    list_display = ("category_path", "category_level_label", "family", "is_active")
    list_filter = ("family", "is_active")
    search_fields = ("name", "parent__name", "parent__parent__name")
    list_select_related = ("parent", "parent__parent")

    class Media:
        js = ("js/admin_category_form.js",)

    @admin.display(description="分类路径", ordering="name")
    def category_path(self, obj):
        return str(obj)

    @admin.display(description="层级")
    def category_level_label(self, obj):
        return f"{obj.category_level} 级"


@admin.register(ExpenseCategory)
class ExpenseCategoryAdmin(admin.ModelAdmin):
    form = ExpenseCategoryAdminForm
    fields = (
        "family",
        "category_level",
        "primary_category",
        "secondary_category",
        "name",
        "is_active",
    )
    list_display = ("category_path", "category_level_label", "family", "is_active")
    list_filter = ("family", "is_active", "parent")
    search_fields = ("name", "parent__name", "parent__parent__name")
    list_select_related = ("parent", "parent__parent")

    class Media:
        js = ("js/admin_category_form.js",)

    @admin.display(description="分类路径", ordering="name")
    def category_path(self, obj):
        return str(obj)

    @admin.display(description="层级")
    def category_level_label(self, obj):
        return f"{obj.category_level} 级"


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
    fields = ("family", "member", "bank_account", "category", "expense_date", "amount", "currency", "remark")
    list_display = ("expense_date", "member", "bank_account", "category", "amount", "currency", "import_batch")
    list_filter = ("family", "member", "bank_account", "category", "currency", "expense_date", "import_batch")
    search_fields = ("remark",)

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        if db_field.name == "currency":
            kwargs["widget"] = forms.Select(choices=CURRENCY_CHOICES)
        return super().formfield_for_dbfield(db_field, request, **kwargs)

    def save_model(self, request, obj, form, change):
        obj.period_start = obj.expense_date
        obj.period_end = obj.expense_date
        if not obj.occurred_at:
            obj.occurred_at = timezone.make_aware(
                datetime.combine(obj.expense_date, time.min)
            )
        obj.merchant = ""
        obj.payment_method = obj.bank_account.account_type_ref.name if obj.bank_account_id else ""
        obj.visibility = VisibilityChoices.PRIVATE
        super().save_model(request, obj, form, change)


@admin.register(ExpenseImportBatch)
class ExpenseImportBatchAdmin(admin.ModelAdmin):
    list_display = (
        "created_at",
        "source_filename",
        "family",
        "row_count",
        "imported_count",
        "skipped_count",
        "total_amount",
        "imported_by",
    )
    list_filter = ("family", "status", "created_at")
    search_fields = ("source_filename", "source_sha256", "worksheet_name")
    readonly_fields = (
        "family",
        "imported_by",
        "source_filename",
        "source_sha256",
        "worksheet_name",
        "row_count",
        "imported_count",
        "skipped_count",
        "total_amount",
        "status",
        "extra_data",
        "created_at",
        "updated_at",
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


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
    fields = ("family", "snapshot_date", "is_draft", "usd_to_base", "hkd_to_base", "remark")
    list_display = ("snapshot_date", "family", "is_draft", "member_balance_summary", "total_balance", "updated_at")
    list_filter = ("family", "is_draft", "snapshot_date")
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
