import calendar

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


class BankAccountForm(BaseModelForm):
    def __init__(self, *args, **kwargs):
        self.request = kwargs.pop("request", None)
        super().__init__(*args, **kwargs)
        default_family = Family.objects.filter(name="我的家庭").first() or Family.objects.first()
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
    return Family.objects.filter(name="我的家庭").first() or Family.objects.first()


def get_current_month_range():
    today = timezone.localdate()
    last_day = calendar.monthrange(today.year, today.month)[1]
    return today.replace(day=1), today.replace(day=last_day)


class IncomeCategoryForm(BaseModelForm):
    class Meta:
        model = IncomeCategory
        fields = ["family", "name", "parent", "is_active"]


class ExpenseCategoryForm(BaseModelForm):
    class Meta:
        model = ExpenseCategory
        fields = ["family", "name", "parent", "is_active"]


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
                last_member_id = self.request.session.get("last_expense_member_id")
                if last_member_id:
                    self.fields["member"].initial = last_member_id

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.expense_date = instance.period_end or timezone.localdate()
        instance.bank_account = None
        instance.merchant = ""
        instance.payment_method = ""
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
            "category",
            "period_start",
            "period_end",
            "amount",
            "currency",
            "remark",
        ]


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
        default_family = Family.objects.filter(name="我的家庭").first() or Family.objects.first()
        if default_family and not self.instance.pk:
            self.fields["family"].initial = default_family
        self.fields["base_currency"].widget = forms.HiddenInput()
        self.fields["base_currency"].initial = "CNY"

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

    class Meta:
        model = AssetBalanceEntry
        fields = [
            "member",
            "account",
            "asset_category",
            "currency",
            "original_amount",
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
