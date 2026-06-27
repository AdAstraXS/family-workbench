from django import forms

from family_core.models import FamilyMember
from ledger.models import BankAccount

from .models import HkIpoListing, HkIpoListingOption, HkIpoSubscriptionTrade


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

        self.fields["final_price"].help_text = "留空时保存会默认按招股价上限计算。"
        self.grouped_fields = [
            {"title": title, "fields": [self[field_name] for field_name in field_names if field_name in self.fields]}
            for title, field_names in self.FIELD_GROUPS
        ]

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

        if self.instance.pk:
            self.initial["listing"] = self.instance.stock_display if hasattr(self.instance, "stock_display") else (
                f"{self.instance.listing.stock_code} "
                f"{self.instance.listing.stock_name or self.instance.listing.company_name}"
            ).strip()

        self.fields["member"].queryset = FamilyMember.objects.filter(is_active=True).order_by("display_name")
        account_queryset = BankAccount.objects.filter(remark__icontains="打新账户", is_active=True).select_related("member").order_by("member__display_name", "account_name")
        member_id = self.data.get("member") if self.is_bound else self.initial.get("member") or self.instance.member_id
        if member_id:
            account_queryset = account_queryset.filter(member_id=member_id)
        self.fields["account"].queryset = account_queryset
        self.fields["account"].label_from_instance = lambda obj: f"{obj.member} - {obj.account_name}"
        self.fields["sell_date"].widget = forms.DateInput(
            attrs={"class": "form-control", "type": "date"}
        )

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
            "financing_amount",
            "financing_rate",
            "financing_days",
            "subscription_fee",
            "allotted_lots",
            "sell_price",
            "sell_date",
            "sold_lots",
            "trading_fee",
            "remark",
        ]

    def clean(self):
        cleaned_data = super().clean()
        allotted_lots = cleaned_data.get("allotted_lots")
        sold_lots = cleaned_data.get("sold_lots") or 0
        sell_date = cleaned_data.get("sell_date")
        if allotted_lots is not None and sold_lots > allotted_lots:
            raise forms.ValidationError("卖出手数不能大于中签手数。")
        if sold_lots and not sell_date:
            self.add_error("sell_date", "录入卖出手数时，请同时填写卖出日期。")
        return cleaned_data
