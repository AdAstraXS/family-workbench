from django import forms
from django.db.models import Q

from family_core.models import AssetCategory, Currency, Family, FamilyMember
from ledger.models import BankAccount

from .account_sync import sync_investment_account
from .models import (
    CashMovementTypeChoices,
    InvestmentAccount,
    InvestmentCashMovement,
    InvestmentPosition,
    InvestmentTransaction,
    InvestmentOption,
    Security,
    TradeTypeChoices,
    WatchlistItem,
)


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
            "bank_account",
            "family",
            "member",
            "account_name",
            "account_no_masked",
            "account_region",
            "visibility",
            "is_active",
            "remark",
        ]


class SecurityForm(BaseModelForm):
    date_fields = ("listing_date",)

    class Meta:
        model = Security
        fields = [
            "asset_category",
            "symbol",
            "name",
            "market",
            "exchange",
            "asset_type",
            "currency",
            "industry",
            "lot_size",
            "listing_date",
            "is_delisted",
            "is_active",
        ]


class InvestmentPositionForm(BaseModelForm):
    date_fields = ("position_date",)

    class Meta:
        model = InvestmentPosition
        fields = [
            "account",
            "security",
            "quantity",
            "avg_cost",
            "diluted_cost",
            "current_price",
            "market_value",
            "unrealized_pnl",
            "realized_pnl",
            "pnl_ratio",
            "position_date",
            "remark",
        ]


class InvestmentTransactionForm(BaseModelForm):
    family = forms.ModelChoiceField(label="家庭", queryset=Family.objects.none())
    member = forms.ModelChoiceField(label="用户", queryset=FamilyMember.objects.none())
    bank_account = forms.ModelChoiceField(
        label="证券账户",
        queryset=BankAccount.objects.none(),
    )
    date_fields = ("trade_date",)

    class Meta:
        model = InvestmentTransaction
        fields = [
            "asset_category",
            "security",
            "trade_date",
            "trade_type_option",
            "currency",
            "quantity",
            "price",
            "amount",
            "fee",
            "tax",
            "trade_logic",
            "information_source_option",
            "strategy_option",
            "strategy_other",
            "emotion_option",
            "exit_condition",
            "remark",
        ]

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.order_fields(
            [
                "family",
                "member",
                "bank_account",
                "asset_category",
                "security",
                "trade_date",
                "trade_type_option",
                "currency",
                "quantity",
                "price",
                "amount",
                "fee",
                "tax",
                "trade_logic",
                "information_source_option",
                "strategy_option",
                "strategy_other",
                "emotion_option",
                "exit_condition",
                "remark",
            ]
        )
        self.fields["security"].label = "交易标的"
        self.fields["trade_type_option"].label = "交易类型"
        self.fields["information_source_option"].label = "信息来源"
        self.fields["strategy_option"].label = "交易类型（策略）"
        self.fields["emotion_option"].label = "交易情绪"
        self.fields["currency"].required = False
        self.fields["currency"].widget = forms.Select(
            choices=[("", "自动根据交易标的")]
            + [
                (item.code, str(item))
                for item in Currency.objects.filter(is_active=True)
            ]
        )
        self.fields["amount"].disabled = True
        self.fields["amount"].help_text = "根据数量 × 价格自动计算"

        login_member = (
            FamilyMember.objects.filter(user=user, is_active=True)
            .select_related("family")
            .first()
            if user
            else None
        )
        family_id = (
            self.data.get("family")
            or self.initial.get("family")
            or (self.instance.account.family_id if self.instance.pk else None)
            or (login_member.family_id if login_member else None)
        )
        member_id = (
            self.data.get("member")
            or self.initial.get("member")
            or (self.instance.account.member_id if self.instance.pk else None)
            or (login_member.pk if login_member else None)
        )
        bank_account_id = (
            self.data.get("bank_account")
            or self.initial.get("bank_account")
            or (
                self.instance.account.bank_account_id
                if self.instance.pk
                else None
            )
        )
        family_queryset = Family.objects.all()
        if user and not user.is_superuser and login_member:
            family_queryset = family_queryset.filter(pk=login_member.family_id)
        self.fields["family"].queryset = family_queryset
        self.fields["family"].initial = family_id
        self.fields["member"].queryset = FamilyMember.objects.filter(
            family_id=family_id,
            is_active=True,
        ).order_by("display_name")
        self.fields["member"].initial = member_id

        account_queryset = BankAccount.objects.filter(
            family_id=family_id,
            member_id=member_id,
            is_active=True,
            account_type_ref__name="券商",
        ).order_by("account_name", "pk")
        if self.instance.pk and bank_account_id:
            account_queryset = BankAccount.objects.filter(
                Q(pk=bank_account_id) | Q(pk__in=account_queryset)
            )
        self.fields["bank_account"].queryset = account_queryset
        self.fields["bank_account"].initial = bank_account_id
        self.fields["asset_category"].queryset = AssetCategory.objects.filter(
            Q(family_id=family_id) | Q(family=None),
            is_active=True,
        ).order_by("display_order", "name")
        watched_ids = WatchlistItem.objects.filter(
            family_id=family_id,
            is_active=True,
        ).values_list("security_id", flat=True)
        security_queryset = Security.objects.filter(
            Q(pk__in=watched_ids)
            | Q(positions__account__family_id=family_id)
        ).distinct().order_by("market", "symbol")
        if self.instance.pk and self.instance.security_id:
            security_queryset = Security.objects.filter(
                Q(pk=self.instance.security_id) | Q(pk__in=security_queryset)
            )
        self.fields["security"].queryset = security_queryset
        self.fields["trade_type_option"].queryset = InvestmentOption.objects.filter(
            category=InvestmentOption.CATEGORY_TRANSACTION_TYPE,
            is_active=True,
        )
        self.fields["information_source_option"].queryset = InvestmentOption.objects.filter(
            category=InvestmentOption.CATEGORY_INFORMATION_SOURCE,
            is_active=True,
        )
        self.fields["strategy_option"].queryset = InvestmentOption.objects.filter(
            category=InvestmentOption.CATEGORY_STRATEGY_TYPE,
            is_active=True,
        )
        self.fields["emotion_option"].queryset = InvestmentOption.objects.filter(
            category=InvestmentOption.CATEGORY_EMOTION,
            is_active=True,
        )

    def clean(self):
        cleaned_data = super().clean()
        security = cleaned_data.get("security")
        bank_account = cleaned_data.get("bank_account")
        family = cleaned_data.get("family")
        member = cleaned_data.get("member")
        trade_type_option = cleaned_data.get("trade_type_option")
        if (
            trade_type_option
            and trade_type_option.code
            in {TradeTypeChoices.BUY, TradeTypeChoices.IPO, TradeTypeChoices.SELL}
            and not security
        ):
            self.add_error("security", "买入、打新和卖出交易必须选择交易标的。")
        if bank_account and family and bank_account.family_id != family.pk:
            self.add_error("bank_account", "证券账户不属于所选家庭。")
        if bank_account and member and bank_account.member_id != member.pk:
            self.add_error("bank_account", "证券账户不属于所选用户。")
        if security and not cleaned_data.get("currency"):
            cleaned_data["currency"] = security.currency
        if security and not cleaned_data.get("asset_category"):
            cleaned_data["asset_category"] = security.asset_category
        if cleaned_data.get("quantity") and cleaned_data.get("price"):
            cleaned_data["amount"] = cleaned_data["quantity"] * cleaned_data["price"]
        strategy = cleaned_data.get("strategy_option")
        if strategy and strategy.code == "other" and not cleaned_data.get("strategy_other"):
            self.add_error("strategy_other", "选择“其他”时请填写具体交易策略。")
        return cleaned_data

    def save(self, commit=True):
        instance = super().save(commit=False)
        bank_account = self.cleaned_data["bank_account"]
        currency = self.cleaned_data.get("currency") or (
            instance.security.currency if instance.security else ""
        )
        account = sync_investment_account(bank_account)
        instance.account = account
        trade_type = self.cleaned_data.get("trade_type_option")
        info_source = self.cleaned_data.get("information_source_option")
        strategy = self.cleaned_data.get("strategy_option")
        emotion = self.cleaned_data.get("emotion_option")
        instance.trade_type = trade_type.code if trade_type else TradeTypeChoices.OTHER
        instance.information_source = info_source.name if info_source else ""
        instance.strategy_type = strategy.name if strategy else ""
        instance.emotion = emotion.name if emotion else ""
        instance.currency = currency
        instance.amount = self.cleaned_data.get("amount") or 0
        if commit:
            instance.save()
            self.save_m2m()
        return instance


class InvestmentCashMovementForm(BaseModelForm):
    family = forms.ModelChoiceField(
        label="家庭",
        queryset=Family.objects.none(),
        disabled=True,
    )
    member = forms.ModelChoiceField(
        label="家庭成员",
        queryset=FamilyMember.objects.none(),
        disabled=True,
    )
    bank_account = forms.ModelChoiceField(
        label="账户名称",
        queryset=BankAccount.objects.none(),
        disabled=True,
    )
    date_fields = ("movement_date",)

    class Meta:
        model = InvestmentCashMovement
        fields = [
            "movement_type",
            "amount",
            "currency",
            "movement_date",
            "counterparty_account",
            "remark",
        ]

    def __init__(self, *args, account=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.account = account
        self.order_fields(
            [
                "family",
                "member",
                "bank_account",
                "movement_type",
                "amount",
                "currency",
                "movement_date",
                "counterparty_account",
                "remark",
            ]
        )
        self.fields["movement_type"].choices = [
            choice
            for choice in CashMovementTypeChoices.choices
            if choice[0]
            in {
                CashMovementTypeChoices.DEPOSIT,
                CashMovementTypeChoices.WITHDRAWAL,
            }
        ]
        self.fields["movement_type"].label = "操作类型"
        self.fields["amount"].label = "金额"
        self.fields["movement_date"].label = "日期"
        self.fields["currency"].widget = forms.Select(
            choices=[(item.code, str(item)) for item in Currency.objects.filter(is_active=True)]
        )
        if account and account.bank_account:
            bank_account = account.bank_account
            self.fields["family"].queryset = Family.objects.filter(
                pk=bank_account.family_id
            )
            self.fields["family"].initial = bank_account.family_id
            self.fields["member"].queryset = FamilyMember.objects.filter(
                pk=bank_account.member_id
            )
            self.fields["member"].initial = bank_account.member_id
            self.fields["bank_account"].queryset = BankAccount.objects.filter(
                pk=bank_account.pk
            )
            self.fields["bank_account"].initial = bank_account.pk
            self.fields["counterparty_account"].queryset = (
                BankAccount.objects.filter(
                    family=bank_account.family,
                    account_type_ref__name="银行",
                    account_region__name="境外",
                    is_active=True,
                )
                .select_related("member", "account_region")
                .order_by("member__display_name", "account_name")
            )
        else:
            self.fields["counterparty_account"].queryset = (
                BankAccount.objects.none()
            )

    def clean(self):
        cleaned_data = super().clean()
        amount = cleaned_data.get("amount")
        movement_type = cleaned_data.get("movement_type")
        if amount is not None:
            if movement_type in {
                CashMovementTypeChoices.DEPOSIT,
                CashMovementTypeChoices.DIVIDEND,
                CashMovementTypeChoices.INTEREST,
            }:
                cleaned_data["amount"] = abs(amount)
            elif movement_type in {
                CashMovementTypeChoices.WITHDRAWAL,
                CashMovementTypeChoices.FEE,
                CashMovementTypeChoices.TAX,
            }:
                cleaned_data["amount"] = -abs(amount)
        return cleaned_data
