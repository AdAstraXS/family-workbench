from decimal import Decimal, ROUND_HALF_UP

from django import forms
from django.db.models import Q

from family_core.models import AssetCategory, Currency, Family, FamilyMember
from family_core.form_widgets import apply_decimal_widgets
from ledger.models import BankAccount

from .account_sync import sync_investment_account
from .models import (
    CashMovementTypeChoices,
    InvestmentAccount,
    InvestmentCashMovement,
    InvestmentPosition,
    InvestmentTransaction,
    InvestmentOption,
    BondDetail,
    OptionContract,
    Security,
    SecurityMarketSnapshot,
    TradeTypeChoices,
    WatchlistItem,
)


class BaseModelForm(forms.ModelForm):
    date_fields = ()
    money_fields = {
        "amount", "fee", "tax", "cash_change", "sell_cost", "realized_pnl",
        "market_value", "unrealized_pnl", "total_cash", "total_market_value",
        "total_asset", "total_cost", "total_pnl",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field_name, field in self.fields.items():
            field.widget.attrs.setdefault("class", "form-control")
            if field_name in self.date_fields:
                field.widget = forms.DateInput(
                    attrs={"class": "form-control", "type": "date"},
                    format="%Y-%m-%d",
                )
        apply_decimal_widgets(self, money_fields=self.money_fields)


class InvestmentAccountForm(BaseModelForm):
    class Meta:
        model = InvestmentAccount
        fields = ["bank_account", "extra_data"]


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

    def __init__(self, *args, **kwargs):
        self.family = kwargs.pop("family", None)
        super().__init__(*args, **kwargs)
        self.fields["asset_type"].label = "金融工具类型"
        self.fields["asset_category"].label = "资产配置类别"
        self.fields["currency"].widget = forms.Select(
            choices=[(item.code, str(item)) for item in Currency.objects.filter(is_active=True)]
        )
        self.fields["asset_category"].queryset = AssetCategory.objects.filter(
            Q(family=self.family) | Q(family=None), is_active=True
        ).order_by("display_order", "name")

    def clean_asset_type(self):
        asset_type = self.cleaned_data["asset_type"]
        if asset_type == Security.TYPE_OPTION:
            raise forms.ValidationError("期权请使用“新增期权合约”页面录入，避免与正股合并。")
        return asset_type

    def save(self, commit=True):
        security = super().save(commit=False)
        if not security.asset_category_id:
            security.asset_category = Security.default_asset_category(
                self.family, security.asset_type
            )
        if commit:
            security.save()
            self.save_m2m()
        return security


class OptionContractForm(forms.Form):
    underlying = forms.ModelChoiceField(label="正股标的", queryset=Security.objects.none())
    contract_symbol = forms.CharField(label="完整合约代码", max_length=30)
    option_type = forms.ChoiceField(label="期权类型", choices=OptionContract.OPTION_TYPE_CHOICES)
    strike_price = forms.DecimalField(label="行权价", max_digits=20, decimal_places=6)
    expiration_date = forms.DateField(
        label="到期日",
        widget=forms.DateInput(attrs={"class": "form-control", "type": "date"}),
    )
    multiplier = forms.IntegerField(label="合约乘数", min_value=1, initial=100)
    market = forms.CharField(label="市场", max_length=20, initial="US")
    currency = forms.ChoiceField(label="交易币种")

    def __init__(self, *args, family=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.family = family
        for field in self.fields.values():
            field.widget.attrs.setdefault("class", "form-control")
        self.fields["underlying"].queryset = Security.objects.exclude(
            asset_type=Security.TYPE_OPTION
        ).order_by("market", "symbol")
        self.fields["currency"].choices = [
            (item.code, str(item)) for item in Currency.objects.filter(is_active=True)
        ]
        apply_decimal_widgets(self)

    def clean_contract_symbol(self):
        symbol = self.cleaned_data["contract_symbol"].strip().upper()
        market = (self.data.get("market") or "US").strip().upper()
        if Security.objects.filter(symbol=symbol, market=market).exists():
            raise forms.ValidationError("该市场已存在相同代码；期权必须使用完整且唯一的合约代码。")
        return symbol

    def clean(self):
        cleaned = super().clean()
        underlying = cleaned.get("underlying")
        currency = cleaned.get("currency")
        if underlying and currency and underlying.currency != currency:
            self.add_error("currency", "期权币种应与正股标的一致。")
        return cleaned

    def save(self, member):
        underlying = self.cleaned_data["underlying"]
        security = Security.objects.create(
            asset_category=Security.default_asset_category(
                member.family, Security.TYPE_OPTION
            ),
            symbol=self.cleaned_data["contract_symbol"],
            name=(
                f"{underlying.name} {self.cleaned_data['expiration_date']} "
                f"{dict(OptionContract.OPTION_TYPE_CHOICES)[self.cleaned_data['option_type']]} "
                f"{self.cleaned_data['strike_price']}"
            ),
            market=self.cleaned_data["market"].strip().upper(),
            exchange=underlying.exchange,
            asset_type=Security.TYPE_OPTION,
            currency=self.cleaned_data["currency"],
            data_source="manual",
        )
        OptionContract.objects.create(
            security=security,
            underlying=underlying,
            option_type=self.cleaned_data["option_type"],
            strike_price=self.cleaned_data["strike_price"],
            expiration_date=self.cleaned_data["expiration_date"],
            multiplier=self.cleaned_data["multiplier"],
        )
        WatchlistItem.objects.update_or_create(
            family=member.family,
            security=security,
            defaults={"member": member, "is_active": True},
        )


class BondForm(forms.Form):
    symbol = forms.CharField(label="债券代码", max_length=30)
    name = forms.CharField(label="债券名称", max_length=200)
    market = forms.CharField(label="市场", max_length=20, initial="US")
    currency = forms.ChoiceField(label="交易币种")
    isin = forms.CharField(label="ISIN", max_length=20, required=False)
    issuer = forms.CharField(label="发行人", max_length=200, required=False)
    bond_type = forms.ChoiceField(label="债券类型", choices=BondDetail.BOND_TYPE_CHOICES)
    face_value = forms.DecimalField(label="单张面值", max_digits=20, decimal_places=4, initial=100)
    coupon_rate = forms.DecimalField(label="票面利率（%）", max_digits=10, decimal_places=6, initial=0)
    coupon_frequency = forms.IntegerField(label="每年付息次数", min_value=1, initial=2)
    maturity_date = forms.DateField(
        label="到期日", required=False, widget=forms.DateInput(attrs={"type": "date"})
    )
    redemption_price = forms.DecimalField(label="到期兑付价格", max_digits=20, decimal_places=6, initial=100)
    quote_basis = forms.ChoiceField(label="报价方式", choices=BondDetail.QUOTE_BASIS_CHOICES)
    clean_price = forms.DecimalField(label="最新净价", max_digits=20, decimal_places=6)
    accrued_interest = forms.DecimalField(
        label="每报价单位应计利息", max_digits=20, decimal_places=6, initial=0
    )
    valuation_date = forms.DateField(
        label="估值日期", required=False, widget=forms.DateInput(attrs={"type": "date"})
    )

    def __init__(self, *args, family=None, instance=None, **kwargs):
        self.family = family
        self.instance = instance
        if instance and not args and "initial" not in kwargs:
            bond = instance.bond_detail
            quote = getattr(instance, "market_snapshot", None)
            kwargs["initial"] = {
                "symbol": instance.symbol,
                "name": instance.name,
                "market": instance.market,
                "currency": instance.currency,
                "isin": bond.isin,
                "issuer": bond.issuer,
                "bond_type": bond.bond_type,
                "face_value": bond.face_value,
                "coupon_rate": bond.coupon_rate,
                "coupon_frequency": bond.coupon_frequency,
                "maturity_date": bond.maturity_date,
                "redemption_price": bond.redemption_price,
                "quote_basis": bond.quote_basis,
                "clean_price": quote.last_price if quote else 0,
                "accrued_interest": bond.accrued_interest,
                "valuation_date": bond.valuation_date,
            }
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.setdefault("class", "form-control")
        self.fields["currency"].choices = [
            (item.code, str(item)) for item in Currency.objects.filter(is_active=True)
        ]
        apply_decimal_widgets(self)

    def clean(self):
        cleaned = super().clean()
        symbol = (cleaned.get("symbol") or "").strip().upper()
        market = (cleaned.get("market") or "").strip().upper()
        duplicate = Security.objects.filter(symbol=symbol, market=market)
        if self.instance:
            duplicate = duplicate.exclude(pk=self.instance.pk)
        if symbol and market and duplicate.exists():
            self.add_error("symbol", "该市场已存在相同代码。")
        return cleaned

    def save(self, member):
        security = self.instance or Security()
        security.asset_category = Security.default_asset_category(
            member.family, Security.TYPE_BOND
        )
        security.symbol = self.cleaned_data["symbol"].strip().upper()
        security.name = self.cleaned_data["name"].strip()
        security.market = self.cleaned_data["market"].strip().upper()
        security.asset_type = Security.TYPE_BOND
        security.currency = self.cleaned_data["currency"]
        security.data_source = "manual"
        security.save()
        BondDetail.objects.update_or_create(
            security=security,
            defaults={
                field: self.cleaned_data[field]
                for field in (
                    "isin", "issuer", "bond_type", "face_value", "coupon_rate",
                    "coupon_frequency", "maturity_date", "redemption_price",
                    "quote_basis", "accrued_interest", "valuation_date",
                )
            },
        )
        SecurityMarketSnapshot.objects.update_or_create(
            security=security,
            defaults={
                "last_price": self.cleaned_data["clean_price"],
                "quote_time": str(self.cleaned_data.get("valuation_date") or ""),
                "raw_data": {"manual_bond_valuation": True},
            },
        )
        WatchlistItem.objects.update_or_create(
            family=member.family,
            security=security,
            defaults={"member": member, "is_active": True},
        )
        return security


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
            "position_effect",
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
                "position_effect",
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
        self.fields["position_effect"].label = "开平仓（期权）"
        self.fields["position_effect"].required = False
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
        self.fields["amount"].help_text = "买入/卖出按数量 × 价格自动计算；股息、利息和费用请直接填写金额。"

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
            supports_investment=True,
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
            | Q(positions__account__bank_account__family_id=family_id)
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
        if security and security.asset_type == Security.TYPE_OPTION:
            if not cleaned_data.get("position_effect"):
                self.add_error("position_effect", "期权交易必须选择开仓或平仓。")
        else:
            cleaned_data["position_effect"] = ""
        if cleaned_data.get("quantity") and cleaned_data.get("price"):
            amount = (
                security.market_value_for(
                    cleaned_data["quantity"],
                    cleaned_data["price"],
                    include_accrued=False,
                )
                if security
                else cleaned_data["quantity"] * cleaned_data["price"]
            )
            cleaned_data["amount"] = amount.quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
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
                CashMovementTypeChoices.EXCHANGE,
                CashMovementTypeChoices.TRANSFER,
                CashMovementTypeChoices.ADJUSTMENT,
            }
        ]
        self.fields["movement_type"].label = "操作类型"
        self.fields["amount"].label = "金额"
        self.fields["movement_date"].label = "日期"
        self.fields["amount"].help_text = "入金填正数、出金自动记为负数；换汇需按卖出和买入币种分别录入两条流水。"
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
                    account_type_ref__code="bank",
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
