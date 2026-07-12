from django import forms

from family_core.models import FamilyMember
from family_core.form_widgets import apply_decimal_widgets
from ledger.models import BankAccount

from .models import HkIpoListing, HkIpoListingOption, HkIpoSubscriptionTrade


MAX_PROSPECTUS_SIZE = 30 * 1024 * 1024


class HkIpoListingForm(forms.ModelForm):
    FIELD_GROUPS = [
        ("基础资料", ["stock_code", "stock_name", "company_name", "listing_type", "mechanism", "sector", "business_summary", "prospectus"]),
        ("发行排期", ["subscription_start_date", "subscription_end_date", "allotment_result_date", "listing_date"]),
        ("价格与规模", [
            "offer_price_min",
            "offer_price_max",
            "final_price",
            "lot_size",
            "global_offer_shares_10k",
            "total_market_cap_100m",
            "h_share_market_cap_100m",
        ]),
        ("发行结构", [
            "sponsor",
            "has_sponsor_dealer",
            "has_greenshoe",
            "stabilizing_manager",
            "has_offer_size_adjustment",
            "offer_size_adjustment_pct",
            "has_cornerstone",
            "cornerstone_investors",
            "cornerstone_pct",
        ]),
        ("估值与决策", [
            "pe_ratio",
            "ps_ratio",
            "comparable_companies",
            "valuation_comment",
            "fundamentals_score",
            "heat_score",
            "subscription_recommendation",
            "decision_reason",
            "remark",
        ]),
    ]
    date_fields = (
        "subscription_start_date",
        "subscription_end_date",
        "allotment_result_date",
        "listing_date",
    )
    textarea_fields = (
        "business_summary",
        "cornerstone_investors",
        "comparable_companies",
        "decision_reason",
        "remark",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        configurable_fields = {
            "listing_type": HkIpoListingOption.CATEGORY_LISTING_TYPE,
            "mechanism": HkIpoListingOption.CATEGORY_MECHANISM,
        }
        for field_name, category in configurable_fields.items():
            current_value = (
                self.data.get(field_name)
                if self.is_bound
                else self.initial.get(field_name) or getattr(self.instance, field_name, "")
            )
            self.fields[field_name] = forms.ChoiceField(
                label=self._meta.model._meta.get_field(field_name).verbose_name,
                choices=HkIpoListingOption.choices_for(category, current_value),
            )

        for field_name, field in self.fields.items():
            field.widget.attrs.setdefault("class", "form-control")
            if field_name in self.date_fields:
                field.widget = forms.DateInput(
                    attrs={"class": "form-control", "type": "date"},
                    format="%Y-%m-%d",
                )
            if field_name in self.textarea_fields:
                field.widget.attrs.update({"rows": 3})
        apply_decimal_widgets(self)

        self.fields["final_price"].help_text = "留空时保存会默认按招股价上限计算。"
        self.grouped_fields = [
            {"title": title, "fields": [self[field_name] for field_name in field_names if field_name in self.fields]}
            for title, field_names in self.FIELD_GROUPS
        ]

    def clean_prospectus(self):
        prospectus = self.cleaned_data.get("prospectus")
        if not prospectus:
            return prospectus
        if getattr(prospectus, "size", 0) > MAX_PROSPECTUS_SIZE:
            raise forms.ValidationError("招股书不能超过 30 MB。")
        content_type = (getattr(prospectus, "content_type", "") or "").lower()
        filename = (getattr(prospectus, "name", "") or "").lower()
        if content_type != "application/pdf" or not filename.endswith(".pdf"):
            raise forms.ValidationError("招股书仅支持 PDF 文件。")
        return prospectus

    class Meta:
        model = HkIpoListing
        fields = [
            "stock_code",
            "stock_name",
            "company_name",
            "listing_type",
            "mechanism",
            "subscription_start_date",
            "subscription_end_date",
            "allotment_result_date",
            "listing_date",
            "offer_price_min",
            "offer_price_max",
            "final_price",
            "lot_size",
            "global_offer_shares_10k",
            "total_market_cap_100m",
            "h_share_market_cap_100m",
            "sector",
            "business_summary",
            "sponsor",
            "has_sponsor_dealer",
            "has_greenshoe",
            "stabilizing_manager",
            "has_offer_size_adjustment",
            "offer_size_adjustment_pct",
            "has_cornerstone",
            "cornerstone_investors",
            "cornerstone_pct",
            "pe_ratio",
            "ps_ratio",
            "comparable_companies",
            "valuation_comment",
            "fundamentals_score",
            "heat_score",
            "subscription_recommendation",
            "decision_reason",
            "prospectus",
            "remark",
        ]


class HkIpoSubscriptionTradeForm(forms.ModelForm):
    listing = forms.CharField(
        label="股票名称/股票代码",
        max_length=200,
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "placeholder": "输入股票名称或股票代码",
                "autocomplete": "off",
            }
        ),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field_name, field in self.fields.items():
            field.widget.attrs.setdefault("class", "form-control")
        apply_decimal_widgets(
            self,
            money_fields={"financing_interest", "subscription_fee"},
        )

        if self.instance.pk:
            self.initial["listing"] = self.instance.stock_display if hasattr(self.instance, "stock_display") else (
                f"{self.instance.listing.stock_code} "
                f"{self.instance.listing.stock_name or self.instance.listing.company_name}"
            ).strip()

        self.fields["member"].queryset = FamilyMember.objects.filter(is_active=True).order_by("display_name")
        account_queryset = BankAccount.objects.filter(supports_ipo=True, is_active=True).select_related("member").order_by("member__display_name", "account_name")
        member_id = self.data.get("member") if self.is_bound else self.initial.get("member") or self.instance.member_id
        if member_id:
            account_queryset = account_queryset.filter(member_id=member_id)
        self.fields["account"].queryset = account_queryset
        self.fields["account"].label_from_instance = lambda obj: f"{obj.member} - {obj.account_name}"

        if self.instance.pk and (self.instance.sold_lots or 0) > 0:
            for field_name in (
                "listing",
                "member",
                "account",
                "tranche",
                "applied_lots",
                "application_method",
            ):
                self.fields[field_name].disabled = True
            self.fields["listing"].help_text = "已有卖出记录；如需修改账户、股票或申购结构，请先撤销卖出记录。"

    def clean_listing(self):
        value = self.cleaned_data["listing"].strip()
        normalized_code = value.upper().replace(" ", "")
        code_candidates = {normalized_code}
        if normalized_code.endswith(".HK"):
            code_candidates.add(normalized_code[:-3])
        elif normalized_code.isdigit():
            code_candidates.add(normalized_code.zfill(5))
            code_candidates.add(f"{normalized_code.zfill(5)}.HK")

        listings = list(HkIpoListing.objects.all())
        exact_matches = [
            listing
            for listing in listings
            if listing.stock_code.upper().replace(" ", "") in code_candidates
            or (listing.stock_name and listing.stock_name.strip().casefold() == value.casefold())
            or (listing.company_name and listing.company_name.strip().casefold() == value.casefold())
        ]
        if len(exact_matches) == 1:
            return exact_matches[0]
        if len(exact_matches) > 1:
            raise forms.ValidationError("匹配到多只新股，请输入完整股票代码。")

        partial_matches = [
            listing
            for listing in listings
            if value.casefold() in (listing.stock_name or "").casefold()
            or value.casefold() in (listing.company_name or "").casefold()
        ]
        if len(partial_matches) == 1:
            return partial_matches[0]
        if len(partial_matches) > 1:
            raise forms.ValidationError("匹配到多只新股，请输入更完整的股票名称或股票代码。")
        raise forms.ValidationError("未找到对应新股，请检查股票名称或股票代码是否已录入新股资料。")

    class Meta:
        model = HkIpoSubscriptionTrade
        fields = [
            "listing",
            "member",
            "account",
            "tranche",
            "applied_lots",
            "application_method",
            "financing_interest",
            "subscription_fee",
            "remark",
        ]


class HkIpoAllotmentForm(forms.ModelForm):
    class Meta:
        model = HkIpoSubscriptionTrade
        fields = ["allotted_lots"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.setdefault("class", "form-control")
        apply_decimal_widgets(self)
        if self.instance.pk and (self.instance.sold_lots or 0) > 0:
            self.fields["allotted_lots"].disabled = True
            self.fields["allotted_lots"].help_text = "已有卖出记录；如需修改中签手数，请先撤销卖出记录。"


class HkIpoSaleForm(forms.Form):
    sell_price = forms.DecimalField(label="卖出金额", max_digits=20, decimal_places=4)
    sell_date = forms.DateField(
        label="卖出日期",
        widget=forms.DateInput(attrs={"class": "form-control", "type": "date"}),
    )
    sold_lots = forms.IntegerField(label="卖出手数", min_value=1)
    trading_fee = forms.DecimalField(
        label="交易费用",
        max_digits=20,
        decimal_places=4,
        initial=0,
    )

    def __init__(self, *args, ipo_trade=None, **kwargs):
        self.ipo_trade = ipo_trade
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.setdefault("class", "form-control")
        apply_decimal_widgets(self, money_fields={"trading_fee"})

    def clean_sold_lots(self):
        sold_lots = self.cleaned_data["sold_lots"]
        if not self.ipo_trade:
            return sold_lots
        already_sold = self.ipo_trade.sold_lots or 0
        allotted_lots = self.ipo_trade.allotted_lots or 0
        current_lots = self.initial.get("sold_lots") or 0
        if already_sold - current_lots + sold_lots > allotted_lots:
            raise forms.ValidationError("卖出手数不能大于剩余持有手数。")
        return sold_lots
