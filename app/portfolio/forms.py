from datetime import datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP

from django import forms
from django.db.models import Q
from django.utils import timezone

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
    PriceSourceChoices,
    Security,
    SecurityExchange,
    SecurityMarket,
    SecurityQuoteConfig,
    TradeTypeChoices,
    WatchlistGroup,
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


def security_market_choices(current_code="", *, excluded_codes=None):
    excluded_codes = set(excluded_codes or ())
    markets = SecurityMarket.objects.filter(
        Q(is_active=True) | Q(code=current_code)
    ).exclude(code__in=excluded_codes)
    return [
        (item.code, f"{item.name}（{item.code}）")
        for item in markets.order_by("display_order", "code")
    ]


def security_exchange_choices(current_market="", current_exchange=""):
    exchanges = (
        SecurityExchange.objects.select_related("market")
        .filter(
            Q(is_active=True, market__is_active=True)
            | Q(market__code=current_market, code=current_exchange)
        )
        .order_by("market__display_order", "display_order", "code")
    )
    groups = []
    grouped = {}
    for item in exchanges:
        key = item.market_id
        if key not in grouped:
            grouped[key] = []
            groups.append(
                (
                    f"{item.market.name}（{item.market.code}）",
                    grouped[key],
                )
            )
        label = f"{item.name}（{item.code}）"
        if item.default_currency:
            label += f" · {item.default_currency}"
        grouped[key].append((f"{item.market.code}:{item.code}", label))
    return [("", "未指定 / 场外交易")] + groups


def clean_market_exchange(form, cleaned):
    market = cleaned.get("market")
    token = cleaned.get("exchange") or ""
    if not token:
        cleaned["exchange"] = ""
        return None
    try:
        token_market, exchange_code = token.split(":", 1)
    except ValueError:
        form.add_error("exchange", "交易所选项无效，请重新选择。")
        return None
    if token_market != market:
        form.add_error("exchange", "所选交易所不属于当前市场，请重新选择。")
        return None
    exchange = SecurityExchange.objects.filter(
        market__code=market,
        code=exchange_code,
    ).first()
    if not exchange:
        form.add_error("exchange", "交易所字典中不存在该选项。")
        return None
    cleaned["exchange"] = exchange.code
    return exchange


ASSET_TYPES_BY_CATEGORY = {
    "equity": {Security.TYPE_STOCK},
    "fixed_income": {Security.TYPE_BOND},
    "fund": {Security.TYPE_ETF, Security.TYPE_FUND},
    "derivatives": {Security.TYPE_OPTION},
    "commodities": {Security.TYPE_OTHER},
    "alternatives": {Security.TYPE_OTHER},
}


def validate_security_market_selection(form, cleaned, exchange):
    if cleaned.get("market") != "CN_B":
        return
    if cleaned.get("asset_type") != Security.TYPE_STOCK:
        form.add_error("asset_type", "B 股市场仅用于股票标的。")
    if not exchange:
        form.add_error("exchange", "B 股必须选择上海或深圳交易所。")
        return
    symbol = (cleaned.get("symbol") or "").strip().upper()
    expected_prefix = "900" if exchange.code == "SH" else "200"
    if symbol and not symbol.startswith(expected_prefix):
        form.add_error(
            "symbol",
            f"{exchange.name}代码应以 {expected_prefix} 开头。",
        )
    if cleaned.get("currency") != exchange.default_currency:
        form.add_error(
            "currency",
            f"{exchange.name}应使用 {exchange.default_currency} 计价。",
        )


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
        current_market = self.instance.market if self.instance and self.instance.pk else ""
        current_exchange = self.instance.exchange if self.instance and self.instance.pk else ""
        self.fields["market"] = forms.ChoiceField(
            label="市场",
            choices=security_market_choices(current_market),
            help_text="选项由后台“证券市场字典”维护。",
        )
        self.fields["exchange"] = forms.ChoiceField(
            label="交易所",
            required=False,
            choices=security_exchange_choices(current_market, current_exchange),
            help_text="选项由后台“证券交易所字典”维护；场外资产可以留空。",
        )
        self.fields["market"].widget.attrs["class"] = "form-control"
        self.fields["exchange"].widget.attrs["class"] = "form-control"
        if current_exchange:
            self.initial["exchange"] = f"{current_market}:{current_exchange}"
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

    def clean(self):
        cleaned = super().clean()
        exchange = clean_market_exchange(self, cleaned)
        validate_security_market_selection(self, cleaned, exchange)
        return cleaned

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


def build_option_contract_symbol(underlying, expiration_date, option_type, strike_price):
    root = "".join(
        character
        for character in underlying.symbol.strip().upper()
        if character.isalnum()
    )
    strike_code = int(
        (Decimal(str(strike_price)) * Decimal("1000")).quantize(Decimal("1"))
    )
    option_code = "C" if option_type == OptionContract.CALL else "P"
    return f"{root}{expiration_date:%y%m%d}{option_code}{strike_code:08d}"


def save_option_contract(
    *,
    member,
    underlying,
    option_type,
    strike_price,
    expiration_date,
    multiplier,
    contract_symbol="",
    security=None,
):
    from .market_data import ensure_quote_config

    contract_symbol = (contract_symbol or build_option_contract_symbol(
        underlying,
        expiration_date,
        option_type,
        strike_price,
    )).strip().upper()
    if security is None:
        existing = (
            OptionContract.objects.select_related("security")
            .filter(
                underlying=underlying,
                option_type=option_type,
                strike_price=strike_price,
                expiration_date=expiration_date,
            )
            .first()
        )
        if existing:
            security = existing.security
    duplicate_symbol = Security.objects.filter(
        symbol=contract_symbol,
        market=underlying.market,
    )
    if security:
        duplicate_symbol = duplicate_symbol.exclude(pk=security.pk)
    if duplicate_symbol.exists():
        raise forms.ValidationError("该市场已存在相同的期权合约代码。")

    security = security or Security()
    security.asset_category = Security.default_asset_category(
        member.family, Security.TYPE_OPTION
    )
    security.symbol = contract_symbol
    security.name = (
        f"{underlying.name} {expiration_date} "
        f"{dict(OptionContract.OPTION_TYPE_CHOICES)[option_type]} {strike_price}"
    )
    security.market = underlying.market
    security.exchange = underlying.exchange
    security.asset_type = Security.TYPE_OPTION
    security.currency = underlying.currency
    security.data_source = "manual"
    security.save()
    OptionContract.objects.update_or_create(
        security=security,
        defaults={
            "underlying": underlying,
            "option_type": option_type,
            "strike_price": strike_price,
            "expiration_date": expiration_date,
            "multiplier": multiplier,
        },
    )
    WatchlistItem.objects.update_or_create(
        family=member.family,
        security=security,
        defaults={"member": member, "is_active": True},
    )
    config = ensure_quote_config(security)
    config.provider = PriceSourceChoices.MANUAL
    config.provider_symbol = ""
    config.price_type = "manual"
    config.max_age_hours = 168
    config.enabled = True
    config.save(
        update_fields=[
            "provider",
            "provider_symbol",
            "price_type",
            "max_age_hours",
            "enabled",
            "updated_at",
        ]
    )
    return security


class OptionContractForm(forms.Form):
    underlying = forms.ModelChoiceField(label="正股标的", queryset=Security.objects.none())
    contract_symbol = forms.CharField(
        label="完整合约代码",
        max_length=30,
        required=False,
        help_text="可以留空，系统会按正股、到期日、期权类型和行权价自动生成。",
    )
    option_type = forms.ChoiceField(label="期权类型", choices=OptionContract.OPTION_TYPE_CHOICES)
    strike_price = forms.DecimalField(label="行权价", max_digits=20, decimal_places=6)
    expiration_date = forms.DateField(
        label="到期日",
        widget=forms.DateInput(attrs={"class": "form-control", "type": "date"}),
    )
    multiplier = forms.IntegerField(label="合约乘数", min_value=1, initial=100)
    market = forms.ChoiceField(label="市场")
    currency = forms.ChoiceField(label="交易币种")

    def __init__(self, *args, family=None, instance=None, **kwargs):
        self.instance = instance
        if instance and not args and "initial" not in kwargs:
            contract = instance.option_contract
            kwargs["initial"] = {
                "underlying": contract.underlying,
                "contract_symbol": instance.symbol,
                "option_type": contract.option_type,
                "strike_price": contract.strike_price,
                "expiration_date": contract.expiration_date,
                "multiplier": contract.multiplier,
                "market": instance.market,
                "currency": instance.currency,
            }
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
        self.fields["market"].choices = security_market_choices()
        self.fields["market"].initial = "US"
        apply_decimal_widgets(self)

    def clean_contract_symbol(self):
        symbol = self.cleaned_data["contract_symbol"].strip().upper()
        if not symbol:
            return symbol
        market = (self.data.get("market") or "US").strip().upper()
        duplicate = Security.objects.filter(symbol=symbol, market=market)
        if self.instance:
            duplicate = duplicate.exclude(pk=self.instance.pk)
        if duplicate.exists():
            raise forms.ValidationError("该市场已存在相同代码；期权必须使用完整且唯一的合约代码。")
        return symbol

    def clean(self):
        cleaned = super().clean()
        underlying = cleaned.get("underlying")
        currency = cleaned.get("currency")
        if underlying and currency and underlying.currency != currency:
            self.add_error("currency", "期权币种应与正股标的一致。")
        if underlying and cleaned.get("market") != underlying.market:
            self.add_error("market", "期权市场必须与正股标的一致。")
        if underlying and all(
            cleaned.get(field) is not None
            for field in ("option_type", "strike_price", "expiration_date")
        ):
            duplicate = OptionContract.objects.filter(
                underlying=underlying,
                option_type=cleaned["option_type"],
                strike_price=cleaned["strike_price"],
                expiration_date=cleaned["expiration_date"],
            )
            if self.instance:
                duplicate = duplicate.exclude(security=self.instance)
            if duplicate.exists():
                self.add_error(None, "相同条款的期权合约已经存在。")
        return cleaned

    def save(self, member):
        return save_option_contract(
            member=member,
            underlying=self.cleaned_data["underlying"],
            option_type=self.cleaned_data["option_type"],
            strike_price=self.cleaned_data["strike_price"],
            expiration_date=self.cleaned_data["expiration_date"],
            multiplier=self.cleaned_data["multiplier"],
            contract_symbol=self.cleaned_data["contract_symbol"],
            security=self.instance,
        )


class WatchlistGroupForm(forms.ModelForm):
    class Meta:
        model = WatchlistGroup
        fields = ["name"]


class OptionPositionActionForm(forms.Form):
    action_date = forms.DateField(
        label="处理日期",
        widget=forms.DateInput(
            attrs={"type": "date", "class": "form-control"},
            format="%Y-%m-%d",
        ),
    )
    quantity = forms.DecimalField(
        label="合约张数",
        max_digits=24,
        decimal_places=6,
        min_value=Decimal("0.000001"),
    )
    fee = forms.DecimalField(
        label="相关费用",
        max_digits=20,
        decimal_places=4,
        min_value=Decimal("0"),
        initial=0,
    )
    remark = forms.CharField(label="备注", required=False, max_length=500)

    def __init__(self, *args, position=None, action="expire", **kwargs):
        self.position = position
        self.action = action
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.setdefault("class", "form-control")
        apply_decimal_widgets(self)

    def clean(self):
        cleaned = super().clean()
        if not self.position:
            return cleaned
        quantity = cleaned.get("quantity")
        if quantity and quantity > abs(self.position.quantity):
            self.add_error("quantity", "处理张数不能超过当前持仓张数。")
        action_date = cleaned.get("action_date")
        contract = self.position.security.option_contract
        if action_date and action_date > timezone.localdate():
            self.add_error("action_date", "处理日期不能晚于今天。")
        if self.action == "expire" and action_date and action_date < contract.expiration_date:
            self.add_error("action_date", "到期作废日期不能早于合约到期日。")
        if self.action in {"exercise", "assignment"}:
            if action_date and action_date > contract.expiration_date:
                self.add_error("action_date", "到期处理日期不能晚于合约到期日。")
        if self.action == "exercise" and self.position.quantity <= 0:
            raise forms.ValidationError("空头期权不能行权，请使用“到期指派”。")
        if self.action == "assignment" and self.position.quantity >= 0:
            raise forms.ValidationError("多头期权不能被指派，请使用“到期行权”。")
        return cleaned


class SecurityQuoteConfigForm(BaseModelForm):
    class Meta:
        model = SecurityQuoteConfig
        fields = [
            "provider",
            "provider_symbol",
            "price_type",
            "max_age_hours",
            "enabled",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["provider"].choices = [
            (PriceSourceChoices.FUTU, PriceSourceChoices.FUTU.label),
            (PriceSourceChoices.MANUAL, PriceSourceChoices.MANUAL.label),
        ]

    def clean(self):
        cleaned = super().clean()
        if (
            cleaned.get("provider") == PriceSourceChoices.FUTU
            and not (cleaned.get("provider_symbol") or "").strip()
        ):
            self.add_error("provider_symbol", "使用 Futu 行情时必须填写行情源代码。")
        return cleaned


class ManualSecurityPriceForm(forms.Form):
    price = forms.DecimalField(
        label="价格",
        max_digits=20,
        decimal_places=6,
        min_value=Decimal("0"),
    )
    price_as_of = forms.DateTimeField(
        label="价格时点",
        widget=forms.DateTimeInput(
            attrs={"type": "datetime-local", "class": "form-control"},
            format="%Y-%m-%dT%H:%M",
        ),
    )
    accrued_interest = forms.DecimalField(
        label="每报价单位应计利息",
        max_digits=20,
        decimal_places=6,
        required=False,
        initial=0,
    )
    remark = forms.CharField(label="价格说明", required=False, max_length=500)

    def __init__(self, *args, security=None, **kwargs):
        self.security = security
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.setdefault("class", "form-control")
        if not security or security.asset_type != Security.TYPE_BOND:
            self.fields.pop("accrued_interest")
        apply_decimal_widgets(self)

    def clean_price(self):
        price = self.cleaned_data["price"]
        if price == 0 and self.security.asset_type != Security.TYPE_OPTION:
            raise forms.ValidationError("除期权外，手工价格必须大于 0。")
        return price

    def clean_price_as_of(self):
        price_as_of = self.cleaned_data["price_as_of"]
        if price_as_of > timezone.now() + timedelta(minutes=5):
            raise forms.ValidationError("价格时点不能晚于当前时间。")
        return price_as_of


def save_bond_security(
    *,
    member,
    symbol,
    name,
    market,
    exchange,
    currency,
    isin,
    issuer,
    bond_type,
    face_value,
    coupon_rate,
    coupon_frequency,
    maturity_date,
    redemption_price,
    quote_basis,
    clean_price,
    accrued_interest,
    valuation_date,
    asset_category=None,
    security=None,
):
    from .market_data import ensure_quote_config, record_security_price

    security = security or Security()
    security.asset_category = (
        asset_category
        if asset_category and asset_category.code == "fixed_income"
        else Security.default_asset_category(member.family, Security.TYPE_BOND)
    )
    security.symbol = symbol.strip().upper()
    security.name = name.strip()
    security.market = market.strip().upper()
    security.exchange = exchange.strip().upper()
    security.asset_type = Security.TYPE_BOND
    security.currency = currency
    security.data_source = "manual"
    security.save()
    BondDetail.objects.update_or_create(
        security=security,
        defaults={
            "isin": isin,
            "issuer": issuer,
            "bond_type": bond_type,
            "face_value": face_value,
            "coupon_rate": coupon_rate,
            "coupon_frequency": coupon_frequency,
            "maturity_date": maturity_date,
            "redemption_price": redemption_price,
            "quote_basis": quote_basis,
            "accrued_interest": accrued_interest,
            "valuation_date": valuation_date,
        },
    )
    config = ensure_quote_config(security)
    config.provider = PriceSourceChoices.MANUAL
    config.provider_symbol = ""
    config.price_type = "manual"
    config.max_age_hours = 720
    config.save(
        update_fields=[
            "provider",
            "provider_symbol",
            "price_type",
            "max_age_hours",
            "updated_at",
        ]
    )
    price_as_of = (
        timezone.make_aware(datetime.combine(valuation_date, time(16, 0)))
        if valuation_date
        else timezone.now()
    )
    record_security_price(
        security,
        clean_price,
        source=PriceSourceChoices.MANUAL,
        price_as_of=price_as_of,
        price_type="manual",
        quote_data={
            "raw_data": {
                "manual_bond_valuation": True,
                "accrued_interest": str(accrued_interest),
            }
        },
    )
    WatchlistItem.objects.update_or_create(
        family=member.family,
        security=security,
        defaults={"member": member, "is_active": True},
    )
    return security


class BondForm(forms.Form):
    symbol = forms.CharField(label="债券代码", max_length=30)
    name = forms.CharField(label="债券名称", max_length=200)
    market = forms.ChoiceField(label="市场")
    exchange = forms.ChoiceField(label="交易所", required=False)
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
                "exchange": (
                    f"{instance.market}:{instance.exchange}"
                    if instance.exchange
                    else ""
                ),
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
        current_market = self.instance.market if self.instance else ""
        current_exchange = self.instance.exchange if self.instance else ""
        self.fields["market"].choices = security_market_choices(
            current_market,
            excluded_codes={"CN_B"},
        )
        self.fields["market"].initial = current_market or "US"
        self.fields["exchange"].choices = security_exchange_choices(
            current_market,
            current_exchange,
        )
        self.fields["currency"].choices = [
            (item.code, str(item)) for item in Currency.objects.filter(is_active=True)
        ]
        apply_decimal_widgets(self)

    def clean(self):
        cleaned = super().clean()
        clean_market_exchange(self, cleaned)
        symbol = (cleaned.get("symbol") or "").strip().upper()
        market = (cleaned.get("market") or "").strip().upper()
        duplicate = Security.objects.filter(symbol=symbol, market=market)
        if self.instance:
            duplicate = duplicate.exclude(pk=self.instance.pk)
        if symbol and market and duplicate.exists():
            self.add_error("symbol", "该市场已存在相同代码。")
        return cleaned

    def save(self, member):
        return save_bond_security(
            member=member,
            security=self.instance,
            symbol=self.cleaned_data["symbol"],
            name=self.cleaned_data["name"],
            market=self.cleaned_data["market"],
            exchange=self.cleaned_data.get("exchange", ""),
            currency=self.cleaned_data["currency"],
            isin=self.cleaned_data["isin"],
            issuer=self.cleaned_data["issuer"],
            bond_type=self.cleaned_data["bond_type"],
            face_value=self.cleaned_data["face_value"],
            coupon_rate=self.cleaned_data["coupon_rate"],
            coupon_frequency=self.cleaned_data["coupon_frequency"],
            maturity_date=self.cleaned_data["maturity_date"],
            redemption_price=self.cleaned_data["redemption_price"],
            quote_basis=self.cleaned_data["quote_basis"],
            clean_price=self.cleaned_data["clean_price"],
            accrued_interest=self.cleaned_data["accrued_interest"],
            valuation_date=self.cleaned_data.get("valuation_date"),
        )


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
    asset_type = forms.ChoiceField(
        label="金融品种",
        choices=[("", "请先选择资产类别")] + list(Security.ASSET_TYPE_CHOICES),
        required=False,
    )
    create_option_contract = forms.BooleanField(
        label="同时新建期权合约",
        required=False,
        help_text="新合约无需先离开交易页面单独建立。",
    )
    option_underlying = forms.ModelChoiceField(
        label="期权正股",
        queryset=Security.objects.none(),
        required=False,
    )
    option_contract_symbol = forms.CharField(
        label="期权合约代码",
        required=False,
        max_length=30,
        help_text="可以留空，由系统自动生成标准代码。",
    )
    option_type = forms.ChoiceField(
        label="期权类型",
        choices=[("", "---------")] + list(OptionContract.OPTION_TYPE_CHOICES),
        required=False,
    )
    option_strike_price = forms.DecimalField(
        label="行权价",
        max_digits=20,
        decimal_places=6,
        required=False,
    )
    option_expiration_date = forms.DateField(
        label="到期日",
        required=False,
        widget=forms.DateInput(attrs={"type": "date", "class": "form-control"}),
    )
    option_multiplier = forms.IntegerField(
        label="合约乘数",
        min_value=1,
        initial=100,
        required=False,
    )
    create_bond = forms.BooleanField(
        label="同时新增债券",
        required=False,
        help_text="新增债券和本次交易会一起保存，任一步失败都不会写入。",
    )
    bond_symbol = forms.CharField(label="债券代码", max_length=30, required=False)
    bond_name = forms.CharField(label="债券名称", max_length=200, required=False)
    bond_market = forms.ChoiceField(label="债券市场", required=False)
    bond_exchange = forms.ChoiceField(label="债券交易所", required=False)
    bond_isin = forms.CharField(label="ISIN", max_length=20, required=False)
    bond_issuer = forms.CharField(label="发行人", max_length=200, required=False)
    bond_type = forms.ChoiceField(
        label="债券类型",
        choices=[("", "---------")] + list(BondDetail.BOND_TYPE_CHOICES),
        required=False,
    )
    bond_face_value = forms.DecimalField(
        label="单张面值", max_digits=20, decimal_places=4, initial=100, required=False
    )
    bond_coupon_rate = forms.DecimalField(
        label="票面利率（%）", max_digits=10, decimal_places=6, initial=0, required=False
    )
    bond_coupon_frequency = forms.IntegerField(
        label="每年付息次数", min_value=1, initial=2, required=False
    )
    bond_maturity_date = forms.DateField(
        label="债券到期日",
        required=False,
        widget=forms.DateInput(attrs={"type": "date", "class": "form-control"}),
    )
    bond_redemption_price = forms.DecimalField(
        label="到期兑付价格", max_digits=20, decimal_places=6, initial=100, required=False
    )
    bond_quote_basis = forms.ChoiceField(
        label="报价方式",
        choices=[("", "---------")] + list(BondDetail.QUOTE_BASIS_CHOICES),
        required=False,
    )
    bond_accrued_interest = forms.DecimalField(
        label="每报价单位应计利息",
        max_digits=20,
        decimal_places=6,
        initial=0,
        required=False,
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
                "asset_type",
                "security",
                "create_option_contract",
                "option_underlying",
                "option_contract_symbol",
                "option_type",
                "option_strike_price",
                "option_expiration_date",
                "option_multiplier",
                "create_bond",
                "bond_symbol",
                "bond_name",
                "bond_market",
                "bond_exchange",
                "bond_isin",
                "bond_issuer",
                "bond_type",
                "bond_face_value",
                "bond_coupon_rate",
                "bond_coupon_frequency",
                "bond_maturity_date",
                "bond_redemption_price",
                "bond_quote_basis",
                "bond_accrued_interest",
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
        self.fields["security"].help_text = "已有标的直接选择；新期权或新债券可在当前页面同时建立。"
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
        self.fields["bond_market"].choices = security_market_choices(
            excluded_codes={"CN_B"},
        )
        self.fields["bond_market"].initial = "US"
        self.fields["bond_exchange"].choices = security_exchange_choices()

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
        selected_asset_type = (
            self.data.get("asset_type")
            or self.initial.get("asset_type")
            or (
                self.instance.security.asset_type
                if self.instance.pk and self.instance.security_id
                else ""
            )
        )
        self.fields["asset_type"].initial = selected_asset_type
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
        self.fields["option_underlying"].queryset = Security.objects.filter(
            Q(watchlist_items__family_id=family_id, watchlist_items__is_active=True)
            | Q(positions__account__bank_account__family_id=family_id)
        ).exclude(asset_type=Security.TYPE_OPTION).distinct().order_by("market", "symbol")
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
        creating_option = cleaned_data.get("create_option_contract")
        creating_bond = cleaned_data.get("create_bond")
        asset_category = cleaned_data.get("asset_category")
        asset_type = cleaned_data.get("asset_type")
        category_code = asset_category.code if asset_category else ""
        if creating_option and creating_bond:
            raise forms.ValidationError("一笔交易不能同时新增期权和债券。")
        if creating_option and (
            category_code != "derivatives" or asset_type != Security.TYPE_OPTION
        ):
            self.add_error("create_option_contract", "请先选择“衍生品 → 期权”。")
        if creating_bond and (
            category_code != "fixed_income" or asset_type != Security.TYPE_BOND
        ):
            self.add_error("create_bond", "请先选择“固定收益类 → 债券”。")
        allowed_asset_types = ASSET_TYPES_BY_CATEGORY.get(category_code)
        if asset_type and allowed_asset_types and asset_type not in allowed_asset_types:
            self.add_error("asset_type", "所选金融品种不属于当前资产类别。")
        if security and asset_type and security.asset_type != asset_type:
            self.add_error("security", "交易标的与所选金融品种不一致，请重新选择。")
        if creating_option:
            required_option_fields = {
                "option_underlying": "请选择期权对应的正股。",
                "option_type": "请选择期权类型。",
                "option_strike_price": "请输入行权价。",
                "option_expiration_date": "请输入到期日。",
                "option_multiplier": "请输入合约乘数。",
            }
            for field_name, message in required_option_fields.items():
                if cleaned_data.get(field_name) in (None, ""):
                    self.add_error(field_name, message)
            underlying = cleaned_data.get("option_underlying")
            if underlying:
                cleaned_data["currency"] = underlying.currency
                cleaned_data["asset_category"] = Security.default_asset_category(
                    family, Security.TYPE_OPTION
                )
            cleaned_data["security"] = None
        if creating_bond:
            required_bond_fields = {
                "bond_symbol": "请输入债券代码。",
                "bond_name": "请输入债券名称。",
                "bond_market": "请选择债券市场。",
                "currency": "请选择债券交易币种。",
                "bond_type": "请选择债券类型。",
                "bond_face_value": "请输入单张面值。",
                "bond_coupon_frequency": "请输入每年付息次数。",
                "bond_redemption_price": "请输入到期兑付价格。",
                "bond_quote_basis": "请选择报价方式。",
            }
            for field_name, message in required_bond_fields.items():
                if cleaned_data.get(field_name) in (None, ""):
                    self.add_error(field_name, message)
            market = (cleaned_data.get("bond_market") or "").strip().upper()
            symbol = (cleaned_data.get("bond_symbol") or "").strip().upper()
            if symbol and market and Security.objects.filter(
                symbol=symbol,
                market=market,
            ).exists():
                self.add_error("bond_symbol", "该市场已存在相同代码，请直接选择已有债券。")
            exchange_token = cleaned_data.get("bond_exchange") or ""
            if exchange_token:
                try:
                    exchange_market, exchange_code = exchange_token.split(":", 1)
                except ValueError:
                    self.add_error("bond_exchange", "债券交易所选项无效，请重新选择。")
                else:
                    if exchange_market != market:
                        self.add_error("bond_exchange", "所选交易所不属于债券市场。")
                    elif not SecurityExchange.objects.filter(
                        market__code=market,
                        code=exchange_code,
                    ).exists():
                        self.add_error("bond_exchange", "交易所字典中不存在该选项。")
                    else:
                        cleaned_data["bond_exchange"] = exchange_code
            if cleaned_data.get("price") is not None and cleaned_data["price"] <= 0:
                self.add_error("price", "债券交易价格必须大于 0。")
            cleaned_data["security"] = None
        if (
            trade_type_option
            and trade_type_option.code
            in {TradeTypeChoices.BUY, TradeTypeChoices.IPO, TradeTypeChoices.SELL}
            and not security
            and not creating_option
            and not creating_bond
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
        if creating_option or (security and security.asset_type == Security.TYPE_OPTION):
            if not cleaned_data.get("position_effect"):
                self.add_error("position_effect", "期权交易必须选择开仓或平仓。")
        else:
            cleaned_data["position_effect"] = ""
        if cleaned_data.get("quantity") and cleaned_data.get("price"):
            if creating_option:
                multiplier = Decimal(str(cleaned_data.get("option_multiplier") or 100))
                amount = (
                    cleaned_data["quantity"]
                    * cleaned_data["price"]
                    * multiplier
                )
            elif creating_bond:
                multiplier = (
                    Decimal("0.01")
                    if cleaned_data.get("bond_quote_basis") == BondDetail.PER_100
                    else Decimal("1")
                )
                amount = cleaned_data["quantity"] * cleaned_data["price"] * multiplier
            else:
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
        if self.cleaned_data.get("create_option_contract"):
            instance.security = save_option_contract(
                member=self.cleaned_data["member"],
                underlying=self.cleaned_data["option_underlying"],
                option_type=self.cleaned_data["option_type"],
                strike_price=self.cleaned_data["option_strike_price"],
                expiration_date=self.cleaned_data["option_expiration_date"],
                multiplier=self.cleaned_data.get("option_multiplier") or 100,
                contract_symbol=self.cleaned_data.get("option_contract_symbol", ""),
            )
        elif self.cleaned_data.get("create_bond"):
            instance.security = save_bond_security(
                member=self.cleaned_data["member"],
                asset_category=self.cleaned_data.get("asset_category"),
                symbol=self.cleaned_data["bond_symbol"],
                name=self.cleaned_data["bond_name"],
                market=self.cleaned_data["bond_market"],
                exchange=self.cleaned_data.get("bond_exchange", ""),
                currency=self.cleaned_data["currency"],
                isin=self.cleaned_data.get("bond_isin", ""),
                issuer=self.cleaned_data.get("bond_issuer", ""),
                bond_type=self.cleaned_data["bond_type"],
                face_value=self.cleaned_data["bond_face_value"],
                coupon_rate=self.cleaned_data.get("bond_coupon_rate") or Decimal("0"),
                coupon_frequency=self.cleaned_data["bond_coupon_frequency"],
                maturity_date=self.cleaned_data.get("bond_maturity_date"),
                redemption_price=self.cleaned_data["bond_redemption_price"],
                quote_basis=self.cleaned_data["bond_quote_basis"],
                clean_price=self.cleaned_data["price"],
                accrued_interest=(
                    self.cleaned_data.get("bond_accrued_interest") or Decimal("0")
                ),
                valuation_date=self.cleaned_data["trade_date"],
            )
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
