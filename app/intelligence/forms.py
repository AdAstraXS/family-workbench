import re
import ipaddress
from urllib.parse import parse_qs, urlsplit

from django import forms

from .models import IntelligenceEvent, IntelligenceSource, IntelligenceSubject


SCORE_CHOICES = (
    (10, "很低（10）"),
    (30, "较低（30）"),
    (50, "一般（50）"),
    (70, "较高（70）"),
    (85, "高（85）"),
    (100, "极高（100）"),
)


class StyledFormMixin:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.setdefault("class", "form-control")


class IntelligenceSubjectForm(StyledFormMixin, forms.ModelForm):
    aliases = forms.CharField(
        label="别名",
        required=False,
        widget=forms.Textarea(attrs={"rows": 3, "placeholder": "Elon Musk\n马斯克"}),
        help_text="每行填写一个中英文名、旧名称或常用简称。",
    )

    class Meta:
        model = IntelligenceSubject
        fields = [
            "subject_type",
            "canonical_name",
            "display_name",
            "category",
            "aliases",
            "profile_summary",
            "avatar_url",
            "importance_level",
            "is_active",
        ]
        widgets = {
            "profile_summary": forms.Textarea(attrs={"rows": 5}),
        }
        help_texts = {
            "canonical_name": "建议使用稳定英文名或机构法定名称；创建后尽量不要修改。",
            "importance_level": "1–5，仅作为信息流排序的人工先验。",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.is_bound and self.instance and self.instance.pk:
            self.initial["aliases"] = "\n".join(self.instance.aliases or [])

    def clean_aliases(self):
        values = re.split(r"[\n,，、]+", self.cleaned_data["aliases"])
        aliases = []
        seen = set()
        for value in values:
            alias = value.strip()
            key = alias.casefold()
            if alias and key not in seen:
                aliases.append(alias)
                seen.add(key)
        return aliases


class IntelligenceSourceForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = IntelligenceSource
        fields = [
            "subject",
            "topics",
            "source_type",
            "source_group",
            "adapter_key",
            "name",
            "url",
            "external_id",
            "source_tier",
            "transport_weight",
            "poll_interval_minutes",
            "is_active",
        ]
        help_texts = {
            "subject": "可选，用于指定默认归属；同一信源仍可关联多个主题。",
            "topics": "可多选人物、机构、行业、技术、政策或证券主题。",
            "adapter_key": "RSS 与 YouTube 官方频道已可采集；YouTube 只读取标题、简介等元数据，不下载视频。",
            "external_id": "YouTube 请填写以 UC 开头的 24 位频道 ID；RSS 可留空。",
            "transport_weight": "同等级来源中，官网可高于官方社交账号。",
        }

    def clean(self):
        cleaned_data = super().clean()
        if not cleaned_data.get("subject") and not cleaned_data.get("topics"):
            self.add_error("topics", "请至少关联一个关注主题。")
        adapter_key = cleaned_data.get("adapter_key")
        source_type = cleaned_data.get("source_type")
        url = (cleaned_data.get("url") or "").strip()
        external_id = (cleaned_data.get("external_id") or "").strip()
        if adapter_key == IntelligenceSource.ADAPTER_RSS:
            if source_type != IntelligenceSource.TYPE_RSS:
                self.add_error("source_type", "RSS 适配器必须选择 RSS / Atom 来源类型。")
            if not url:
                self.add_error("url", "RSS 适配器必须填写公开订阅地址。")
        elif adapter_key == IntelligenceSource.ADAPTER_YOUTUBE:
            if source_type != IntelligenceSource.TYPE_YOUTUBE:
                self.add_error("source_type", "YouTube 适配器必须选择 YouTube 来源类型。")
            if not external_id and url:
                external_id = parse_qs(urlsplit(url).query).get("channel_id", [""])[0]
                cleaned_data["external_id"] = external_id
                self.instance.external_id = external_id
            if not re.fullmatch(r"UC[A-Za-z0-9_-]{22}", external_id):
                self.add_error("external_id", "请填写以 UC 开头的 24 位 YouTube 频道 ID，不要填写频道昵称。")
        if adapter_key in {IntelligenceSource.ADAPTER_RSS, IntelligenceSource.ADAPTER_YOUTUBE} and url:
            parsed = urlsplit(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                self.add_error("url", "自动信源只能使用公开的 HTTP 或 HTTPS 地址。")
            else:
                if parsed.username or parsed.password:
                    self.add_error("url", "自动信源地址不能包含用户名或密码。")
                try:
                    port = parsed.port
                except ValueError:
                    port = -1
                if port not in {None, 80, 443}:
                    self.add_error("url", "自动信源只能使用标准 HTTP/HTTPS 端口。")
                hostname = parsed.hostname.casefold()
                if hostname == "localhost" or hostname.endswith((".localhost", ".local", ".internal")):
                    self.add_error("url", "自动信源不能指向本机或内网地址。")
                try:
                    address = ipaddress.ip_address(hostname)
                except ValueError:
                    address = None
                if address is not None and not address.is_global:
                    self.add_error("url", "自动信源不能指向本机或内网地址。")
        return cleaned_data

    def save(self, commit=True):
        source = super().save(commit=commit)
        if commit and source.subject_id:
            source.topics.add(source.subject_id)
        return source


class ManualEventForm(StyledFormMixin, forms.Form):
    subject = forms.ModelChoiceField(label="主要关注主题", queryset=IntelligenceSubject.objects.none())
    event_type = forms.ChoiceField(label="事件类型", choices=IntelligenceEvent.TYPE_CHOICES)
    title = forms.CharField(label="事件标题", max_length=500)
    occurred_at = forms.DateTimeField(
        label="事件时间",
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
        input_formats=["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S"],
    )
    occurred_precision = forms.ChoiceField(label="时间精度", choices=IntelligenceEvent.PRECISION_CHOICES)
    summary = forms.CharField(label="事实摘要", widget=forms.Textarea(attrs={"rows": 6}))
    why_it_matters = forms.CharField(
        label="为什么重要",
        required=False,
        widget=forms.Textarea(attrs={"rows": 4}),
    )
    relevance_score = forms.TypedChoiceField(
        label="相关性",
        choices=SCORE_CHOICES,
        coerce=int,
        initial=70,
        help_text="与投资、科技、公司经营、产业或政策判断的相关程度。",
    )
    impact_score = forms.TypedChoiceField(label="影响程度", choices=SCORE_CHOICES, coerce=int, initial=50)
    novelty_score = forms.TypedChoiceField(label="新颖性", choices=SCORE_CHOICES, coerce=int, initial=50)
    actionability_score = forms.TypedChoiceField(
        label="投资参考价值",
        choices=SCORE_CHOICES,
        coerce=int,
        initial=50,
    )
    timeliness_score = forms.TypedChoiceField(label="时效性", choices=SCORE_CHOICES, coerce=int, initial=70)
    change_type = forms.ChoiceField(label="变化信号", choices=IntelligenceEvent.CHANGE_CHOICES)
    review_status = forms.ChoiceField(label="复核状态", choices=IntelligenceEvent.REVIEW_CHOICES)

    source_name = forms.CharField(label="来源名称", max_length=200)
    source_tier = forms.ChoiceField(label="来源等级", choices=IntelligenceSource.TIER_CHOICES)
    source_group = forms.ChoiceField(label="信源类别", choices=IntelligenceSource.GROUP_CHOICES)
    source_title = forms.CharField(label="原文标题", max_length=500)
    source_url = forms.URLField(label="原文链接", max_length=1000)
    source_author = forms.CharField(label="发布者", max_length=200, required=False)
    evidence_type = forms.ChoiceField(label="证据类型", choices=(
        ("fact", "事实证据"),
        ("opinion", "观点证据"),
        ("context", "背景材料"),
    ))
    evidence_excerpt = forms.CharField(
        label="必要短摘录",
        required=False,
        widget=forms.Textarea(attrs={"rows": 4}),
        help_text="只填写核查所需的短摘录，不复制完整版权文章。",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["subject"].queryset = IntelligenceSubject.objects.filter(is_active=True)
        for field in self.fields.values():
            field.widget.attrs.setdefault("class", "form-control")


class IntelligenceEventForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = IntelligenceEvent
        fields = [
            "event_type",
            "title",
            "occurred_at",
            "occurred_precision",
            "summary",
            "why_it_matters",
            "relevance_score",
            "impact_score",
            "novelty_score",
            "actionability_score",
            "timeliness_score",
            "change_type",
            "review_status",
        ]
        widgets = {
            "occurred_at": forms.DateTimeInput(
                attrs={"type": "datetime-local"},
                format="%Y-%m-%dT%H:%M",
            ),
            "summary": forms.Textarea(attrs={"rows": 7}),
            "why_it_matters": forms.Textarea(attrs={"rows": 5}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["occurred_at"].input_formats = ["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S"]
