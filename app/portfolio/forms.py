from django import forms

from .models import InvestmentAccount, InvestmentPosition, InvestmentTransaction, Security


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


class InvestmentAccountForm(BaseModelForm):
    class Meta:
        model = InvestmentAccount
        fields = [
            "family",
            "member",
            "broker_name",
            "account_name",
            "account_no_masked",
            "market_scope",
            "currency",
            "cash_balance",
            "visibility",
            "is_active",
            "remark",
        ]


class SecurityForm(BaseModelForm):
    class Meta:
        model = Security
        fields = ["symbol", "name", "market", "asset_type", "currency", "industry", "is_active"]


class InvestmentPositionForm(BaseModelForm):
    date_fields = ("position_date",)

    class Meta:
        model = InvestmentPosition
        fields = [
            "account",
            "security",
            "quantity",
            "avg_cost",
            "current_price",
            "market_value",
            "unrealized_pnl",
            "pnl_ratio",
            "position_date",
            "remark",
        ]


class InvestmentTransactionForm(BaseModelForm):
    date_fields = ("trade_date",)

    class Meta:
        model = InvestmentTransaction
        fields = [
            "account",
            "security",
            "trade_date",
            "trade_type",
            "quantity",
            "price",
            "amount",
            "fee",
            "tax",
            "currency",
            "realized_pnl",
            "remark",
        ]
