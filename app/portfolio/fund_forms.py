from django import forms
from django.db.models import Q
from django.utils import timezone
from family_core.models import FamilyMember
from .fund_models import FUND_CODE, FundDcaPlan


class FundDcaForm(forms.ModelForm):
    fund_code = forms.CharField(label="基金代码", min_length=6, max_length=6, validators=[FUND_CODE],
        help_text="六位代码，区分A/C份额。保存后自动抓取基金名称、净值和资料。",
        widget=forms.TextInput(attrs={"inputmode": "numeric", "pattern": "[0-9]{6}", "placeholder": "例如 016452"}))
    revision = forms.IntegerField(widget=forms.HiddenInput, required=False)

    class Meta:
        model = FundDcaPlan
        fields = ["name", "member", "fund_code", "start_date", "end_date", "frequency", "amount",
                  "fee_rate", "balance", "balance_date", "remark", "revision"]
        widgets = {key: forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"})
                   for key in ["start_date", "end_date", "balance_date"]}
        widgets["remark"] = forms.Textarea(attrs={"rows": 3})
        help_texts = {"fee_rate": "填写支付宝实际费率，例如0.1表示0.1%。基金公开费率不会覆盖此值。",
                     "balance": "手动填写支付宝显示的余额；未知时留空。0表示已确认余额为0。",
                     "end_date": "可留空；起止日期不参与年度计划额折算。"}

    def __init__(self, *args, family, actor_member, can_manage_all=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance.family = family
        members = FamilyMember.objects.filter(family=family, is_active=True)
        if not can_manage_all:
            members = members.filter(pk=actor_member.pk)
        elif self.instance.pk:
            members = FamilyMember.objects.filter(family=family).filter(
                Q(is_active=True) | Q(pk=self.instance.member_id))
        self.fields["member"].queryset = members
        self.fields["member"].empty_label = None
        self.initial.setdefault("member", actor_member.pk if actor_member else members.first())
        self.initial.setdefault("start_date", timezone.localdate())
        self.initial.setdefault("fee_rate", "0")
        if self.instance.pk:
            self.initial["fund_code"] = self.instance.fund.code
            self.initial["revision"] = self.instance.revision

    def clean(self):
        cleaned = super().clean()
        if self.instance.pk and not cleaned.get("revision"):
            self.add_error("revision", "页面版本缺失，请重新打开编辑页面。")
        return cleaned


class FundBalanceForm(forms.ModelForm):
    revision = forms.IntegerField(widget=forms.HiddenInput)

    class Meta:
        model = FundDcaPlan
        fields = ["balance", "balance_date", "revision"]
        widgets = {"balance_date": forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"})}
        help_texts = {"balance": "填0表示已确认余额为0；余额和日期同时留空表示未知。"}

