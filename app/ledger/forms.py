import calendar
from datetime import datetime, time
from decimal import Decimal, InvalidOperation

from django import forms
from django.forms import inlineformset_factory
from django.utils import timezone

from .models import (
    AnnualBudget,
    AnnualBudgetLine,
    AssetBalanceEntry,
    AssetBalanceSnapshot,
    BankAccount,
    ExpenseCategory,
    ExpenseRecord,
    IncomeCategory,
    IncomeRecord,
)
from family_core.models import Family, FamilyMember
from family_core.household import get_household_family


class TwoDecimalNumberInput(forms.NumberInput):
    def format_value(self, value):
        if value in (None, ""):
            return ""
        try:
            return f"{Decimal(str(value)):.2f}"
        except (InvalidOperation, TypeError, ValueError):
            return value


class BaseModelForm(forms.ModelForm):
    date_fields = ()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field_name, field in self.fields.items():
            field.widget.attrs.setdefault("class", "form-control")
            if field_name in self.date_fields:
                field.widget = forms.DateInput(
                    attrs={"class": "form-control", "type": "date"},
                    format="%Y-%m-%d",
                )
            elif isinstance(field, forms.DecimalField):
                attrs = dict(field.widget.attrs)
                attrs.update({"class": "form-control", "step": "0.01"})
                field.widget = TwoDecimalNumberInput(attrs=attrs)


class BankAccountForm(BaseModelForm):
    def __init__(self, *args, **kwargs):
        self.request = kwargs.pop("request", None)
        super().__init__(*args, **kwargs)
        default_family = get_household_family()
        if default_family and not self.instance.pk:
            self.fields["family"].initial = default_family
            self.fields["member"].queryset = FamilyMember.objects.filter(family=default_family, is_active=True)
        if self.request and not self.instance.pk:
            last_member_id = self.request.session.get("last_account_member_id")
            if last_member_id:
                self.fields["member"].initial = last_member_id

    class Meta:
        model = BankAccount
        fields = [
            "family",
            "member",
            "account_name",
            "account_no_masked",
            "account_region",
            "account_type_ref",
            "supports_investment",
            "supports_ipo",
            "is_active",
            "remark",
        ]


class CurrencyChoiceMixin:
    CURRENCY_CHOICES = [
        ("CNY", "人民币 CNY"),
        ("HKD", "港币 HKD"),
        ("USD", "美元 USD"),
    ]

    def apply_currency_choices(self):
        self.fields["currency"].widget = forms.Select(choices=self.CURRENCY_CHOICES, attrs={"class": "form-control"})


def get_default_family():
    return get_household_family()


def get_current_month_range():
    today = timezone.localdate()
    last_day = calendar.monthrange(today.year, today.month)[1]
    return today.replace(day=1), today.replace(day=last_day)


class ExpenseAccountSelect(forms.Select):
    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        option = super().create_option(name, value, label, selected, index, subindex=subindex, attrs=attrs)
        if value and hasattr(value, "instance"):
            option["attrs"]["data-member-id"] = str(value.instance.member_id)
        return option


class ExpenseCategorySelect(forms.Select):
    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        option = super().create_option(name, value, label, selected, index, subindex=subindex, attrs=attrs)
        if value and hasattr(value, "instance"):
            option["attrs"]["data-parent-id"] = str(value.instance.parent_id or "")
        return option


class ExpenseCategoryChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return obj.name


class IncomeRecordForm(CurrencyChoiceMixin, BaseModelForm):
    date_fields = ("period_start", "period_end")

    def __init__(self, *args, **kwargs):
        self.request = kwargs.pop("request", None)
        super().__init__(*args, **kwargs)
        self.apply_currency_choices()
        default_family = get_default_family()
        period_start, period_end = get_current_month_range()
        if not self.instance.pk:
            if default_family:
                self.fields["family"].initial = default_family
                self.fields["member"].queryset = FamilyMember.objects.filter(family=default_family, is_active=True)
            self.fields["period_start"].initial = period_start
            self.fields["period_end"].initial = period_end
            if self.request:
                last_member_id = self.request.session.get("last_income_member_id")
                if last_member_id:
                    self.fields["member"].initial = last_member_id

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.income_date = instance.period_end or timezone.localdate()
        instance.bank_account = None
        instance.source_name = ""
        instance.visibility = "private"
        if commit:
            instance.save()
            self.save_m2m()
        return instance

    class Meta:
        model = IncomeRecord
        fields = [
            "family",
            "member",
            "category",
            "period_start",
            "period_end",
            "amount",
            "currency",
            "is_recurring",
            "remark",
        ]


class ExpenseRecordForm(CurrencyChoiceMixin, BaseModelForm):
    date_fields = ("expense_date",)
    ALLOWED_ACCOUNT_TYPE_CODES = ("bank", "wechat", "alipay")
    primary_category = ExpenseCategoryChoiceField(
        label="一级分类",
        queryset=ExpenseCategory.objects.none(),
        widget=ExpenseCategorySelect(attrs={"class": "form-control"}),
    )
    secondary_category = ExpenseCategoryChoiceField(
        label="二级分类",
        queryset=ExpenseCategory.objects.none(),
        widget=ExpenseCategorySelect(attrs={"class": "form-control"}),
    )
    tertiary_category = ExpenseCategoryChoiceField(
        label="三级分类",
        queryset=ExpenseCategory.objects.none(),
        required=False,
        widget=ExpenseCategorySelect(attrs={"class": "form-control"}),
        help_text="没有三级分类时可以留空。",
    )

    def __init__(self, *args, **kwargs):
        self.request = kwargs.pop("request", None)
        super().__init__(*args, **kwargs)
        self.apply_currency_choices()
        default_family = get_default_family()
        account_queryset = BankAccount.objects.filter(
            is_active=True,
            account_type_ref__code__in=self.ALLOWED_ACCOUNT_TYPE_CODES,
            account_type_ref__is_active=True,
        ).select_related("member", "account_type_ref").order_by(
            "member__display_name",
            "account_type_ref__display_order",
            "account_name",
        )
        if not self.instance.pk:
            if default_family:
                self.fields["family"].initial = default_family
                self.fields["member"].queryset = FamilyMember.objects.filter(family=default_family, is_active=True)
                account_queryset = account_queryset.filter(family=default_family)
                self._set_category_querysets(default_family)
            self.fields["expense_date"].initial = timezone.localdate()
            if self.request:
                last_member_id = self.request.session.get("last_expense_member_id")
                if last_member_id:
                    self.fields["member"].initial = last_member_id
        elif self.instance.family_id:
            account_queryset = account_queryset.filter(family_id=self.instance.family_id)
            self.fields["member"].queryset = FamilyMember.objects.filter(
                family_id=self.instance.family_id,
                is_active=True,
            )
            self._set_category_querysets(self.instance.family)
            path = []
            category = self.instance.category
            while category:
                path.append(category)
                category = category.parent
            path.reverse()
            if path:
                self.fields["primary_category"].initial = path[0]
            if len(path) > 1:
                self.fields["secondary_category"].initial = path[1]
            if len(path) > 2:
                self.fields["tertiary_category"].initial = path[2]
        self.fields["bank_account"].widget = ExpenseAccountSelect(attrs={"class": "form-control"})
        self.fields["bank_account"].queryset = account_queryset
        self.fields["bank_account"].required = True
        self.fields["bank_account"].help_text = "仅显示所选成员名下的银行、微信和支付宝账户。"

    def _set_category_querysets(self, family):
        categories = ExpenseCategory.objects.filter(family=family, is_active=True)
        self.fields["primary_category"].queryset = categories.filter(parent__isnull=True).order_by("name")
        self.fields["secondary_category"].queryset = categories.filter(
            parent__isnull=False,
            parent__parent__isnull=True,
        ).select_related("parent").order_by("parent__name", "name")
        self.fields["tertiary_category"].queryset = categories.filter(
            parent__isnull=False,
            parent__parent__isnull=False,
            parent__parent__parent__isnull=True,
        ).select_related("parent", "parent__parent").order_by(
            "parent__parent__name", "parent__name", "name"
        )

    def clean(self):
        cleaned_data = super().clean()
        family = cleaned_data.get("family")
        member = cleaned_data.get("member")
        account = cleaned_data.get("bank_account")
        primary_category = cleaned_data.get("primary_category")
        secondary_category = cleaned_data.get("secondary_category")
        tertiary_category = cleaned_data.get("tertiary_category")
        if account:
            if account.account_type_ref is None or account.account_type_ref.code not in self.ALLOWED_ACCOUNT_TYPE_CODES:
                self.add_error("bank_account", "支出账户仅限银行、微信或支付宝账户。")
            if family and account.family_id != family.id:
                self.add_error("bank_account", "支出账户必须属于所选家庭。")
            if member and account.member_id != member.id:
                self.add_error("bank_account", "支出账户必须属于所选成员。")
        for field_name, category in (
            ("primary_category", primary_category),
            ("secondary_category", secondary_category),
            ("tertiary_category", tertiary_category),
        ):
            if category and family and category.family_id != family.id:
                self.add_error(field_name, "支出分类必须属于所选家庭。")
        if primary_category and secondary_category and secondary_category.parent_id != primary_category.id:
            self.add_error("secondary_category", "二级分类必须属于所选一级分类。")
        if tertiary_category and secondary_category and tertiary_category.parent_id != secondary_category.id:
            self.add_error("tertiary_category", "三级分类必须属于所选二级分类。")
        return cleaned_data

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.category = (
            self.cleaned_data.get("tertiary_category")
            or self.cleaned_data["secondary_category"]
        )
        instance.period_start = instance.expense_date
        instance.period_end = instance.expense_date
        if not instance.occurred_at:
            instance.occurred_at = timezone.make_aware(
                datetime.combine(instance.expense_date, time.min)
            )
        instance.merchant = ""
        instance.payment_method = instance.bank_account.account_type_ref.name if instance.bank_account_id else ""
        instance.visibility = "private"
        if commit:
            instance.save()
            self.save_m2m()
        return instance

    class Meta:
        model = ExpenseRecord
        fields = [
            "family",
            "member",
            "bank_account",
            "primary_category",
            "secondary_category",
            "tertiary_category",
            "expense_date",
            "amount",
            "currency",
            "remark",
        ]


class ExpenseImportForm(forms.Form):
    family = forms.ModelChoiceField(label="所属家庭", queryset=Family.objects.none())
    workbook = forms.FileField(
        label="支出 Excel",
        help_text="仅支持 .xlsx；首行必须是固定的 8 列表头。",
        widget=forms.ClearableFileInput(attrs={"accept": ".xlsx", "class": "form-control"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["family"].queryset = Family.objects.order_by("name")
        self.fields["family"].widget.attrs["class"] = "form-control"
        default_family = get_default_family()
        if default_family:
            self.fields["family"].initial = default_family

    def clean_workbook(self):
        workbook = self.cleaned_data["workbook"]
        if not workbook.name.lower().endswith(".xlsx"):
            raise forms.ValidationError("请选择 .xlsx 格式的支出文件。")
        if workbook.size > 10 * 1024 * 1024:
            raise forms.ValidationError("文件不能超过 10 MB。")
        return workbook


class AnnualBudgetForm(BaseModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        default_family = get_default_family()
        if default_family and not self.instance.pk:
            self.fields["family"].initial = default_family
        if not self.instance.pk:
            self.fields["year"].initial = timezone.localdate().year

    class Meta:
        model = AnnualBudget
        fields = ["family", "year", "remark"]


class AnnualBudgetLineForm(BaseModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        default_family = get_default_family()
        if default_family:
            self.fields["income_category"].queryset = IncomeCategory.objects.filter(family=default_family, is_active=True).select_related("parent")
            self.fields["expense_category"].queryset = ExpenseCategory.objects.filter(family=default_family, is_active=True).select_related("parent")
        self.fields["remark"].widget.attrs.update({"rows": 1})
        self.fields["remark"].widget.attrs["class"] = "form-control compact-textarea"

    class Meta:
        model = AnnualBudgetLine
        fields = [
            "line_type",
            "income_category",
            "expense_category",
            "annual_amount",
            "remark",
        ]

    def clean(self):
        cleaned_data = super().clean()
        line_type = cleaned_data.get("line_type")
        income_category = cleaned_data.get("income_category")
        expense_category = cleaned_data.get("expense_category")
        if line_type == AnnualBudgetLine.LINE_TYPE_INCOME:
            cleaned_data["expense_category"] = None
            if not income_category:
                raise forms.ValidationError("收入预算需要选择收入分类。")
        elif line_type == AnnualBudgetLine.LINE_TYPE_EXPENSE:
            cleaned_data["income_category"] = None
            if not expense_category:
                raise forms.ValidationError("支出预算需要选择支出分类。")
        return cleaned_data


def make_annual_budget_line_formset(extra=6):
    return inlineformset_factory(
        AnnualBudget,
        AnnualBudgetLine,
        form=AnnualBudgetLineForm,
        extra=extra,
        can_delete=True,
    )


AnnualBudgetLineFormSet = make_annual_budget_line_formset(extra=0)


class AssetBalanceSnapshotForm(BaseModelForm):
    date_fields = ("snapshot_date",)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        default_family = get_household_family()
        if default_family and not self.instance.pk:
            self.fields["family"].initial = default_family
            self.initial.setdefault("family", default_family.pk)
        self.fields["base_currency"].widget = forms.HiddenInput()
        self.fields["base_currency"].initial = "CNY"
        self.initial.setdefault("base_currency", "CNY")

    class Meta:
        model = AssetBalanceSnapshot
        fields = ["family", "snapshot_date", "base_currency", "usd_to_base", "hkd_to_base", "remark"]

    def clean_base_currency(self):
        return "CNY"


class AssetBalanceEntryForm(CurrencyChoiceMixin, BaseModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.apply_currency_choices()
        self.fields["remark"].widget.attrs.update({"rows": 1})
        self.fields["remark"].widget.attrs["class"] = "form-control compact-textarea"
        self.fields["display_order"].widget = forms.HiddenInput()

    class Meta:
        model = AssetBalanceEntry
        fields = [
            "member",
            "account",
            "asset_category",
            "currency",
            "original_amount",
            "display_order",
            "remark",
        ]

    def clean(self):
        cleaned_data = super().clean()
        member = cleaned_data.get("member")
        account = cleaned_data.get("account")
        if member and account and account.member_id != member.id:
            raise forms.ValidationError("账户必须属于当前选择的成员。")
        return cleaned_data


def make_asset_balance_entry_formset(extra=0):
    return inlineformset_factory(
        AssetBalanceSnapshot,
        AssetBalanceEntry,
        form=AssetBalanceEntryForm,
        extra=extra,
        can_delete=True,
    )


AssetBalanceEntryFormSet = make_asset_balance_entry_formset(extra=0)
