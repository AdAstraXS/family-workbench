"""投研模块表单。

owner/family 由后端从当前登录成员赋值，不进入表单；
关键假设/待验证问题用多行文本（每行一条），空输入转换为 []，
最终传给服务的是 list。编辑表单不开放原始理由与证券，
expected_revision_id 是必填 HiddenInput：缺失或非数字直接拒绝，
不能默认为最新版本。
"""
from django import forms

from portfolio.models import Security


def parse_list_field(raw):
    """多行文本 → list：去首尾空白、去掉空行；空输入返回 []。"""
    if not raw:
        return []
    return [line.strip() for line in raw.splitlines() if line.strip()]


class ResearchFormMixin:
    """多行列表字段的公共清理：转成 list 供服务校验。"""

    def clean(self):
        cleaned = super().clean()
        for field in ("pillars", "questions"):
            if field in self.fields:
                cleaned[field] = parse_list_field(cleaned.get(field))
        return cleaned


class CreateDossierForm(ResearchFormMixin, forms.Form):
    security = forms.ModelChoiceField(
        label="研究标的",
        queryset=Security.objects.order_by("symbol", "pk"),
    )
    initial_thesis = forms.CharField(
        label="原始持有理由",
        widget=forms.Textarea(attrs={"rows": 4}),
    )
    pillars = forms.CharField(
        label="关键假设（每行一条，最多 5 条）",
        required=False,
        widget=forms.Textarea(
            attrs={
                "rows": 3,
                "placeholder": "当前暂时采用、会直接影响估值或判断的前提，例如：未来三年 EPS 年增长率约 12%。",
            }
        ),
    )
    questions = forms.CharField(
        label="待验证问题（每行一条，最多 5 条）",
        required=False,
        widget=forms.Textarea(
            attrs={
                "rows": 3,
                "placeholder": "还需要通过财报、公告或后续跟踪确认的问题，例如：资本支出转化为收入和现金流的速度如何？",
            }
        ),
    )


class ExploreDossierForm(forms.Form):
    security = forms.ModelChoiceField(
        label="想了解的美股", queryset=Security.objects.filter(market="US", asset_type=Security.TYPE_STOCK).order_by("symbol", "pk"),
    )


class FirstThesisForm(ResearchFormMixin, forms.Form):
    thesis = forms.CharField(label="我的第一版判断", widget=forms.Textarea(attrs={"rows": 4}))
    pillars = forms.CharField(label="关键假设（每行一条，最多 5 条）", required=False, widget=forms.Textarea(attrs={"rows": 3}))
    questions = forms.CharField(label="待验证问题（每行一条，最多 5 条）", required=False, widget=forms.Textarea(attrs={"rows": 3}))


class EditThesisForm(ResearchFormMixin, forms.Form):
    thesis = forms.CharField(
        label="当前判断",
        widget=forms.Textarea(attrs={"rows": 4}),
    )
    pillars = forms.CharField(
        label="关键假设（每行一条，最多 5 条）",
        required=False,
        widget=forms.Textarea(
            attrs={
                "rows": 3,
                "placeholder": "当前暂时采用、会直接影响估值或判断的前提，例如：未来三年 EPS 年增长率约 12%。",
            }
        ),
    )
    questions = forms.CharField(
        label="待验证问题（每行一条，最多 5 条）",
        required=False,
        widget=forms.Textarea(
            attrs={
                "rows": 3,
                "placeholder": "还需要通过财报、公告或后续跟踪确认的问题，例如：资本支出转化为收入和现金流的速度如何？",
            }
        ),
    )
    change_reason = forms.CharField(
        label="修改原因",
        max_length=500,
        widget=forms.TextInput,
    )
    expected_revision_id = forms.IntegerField(
        label="当前版本号",
        required=True,
        widget=forms.HiddenInput,
    )
