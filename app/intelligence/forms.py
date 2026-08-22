import re
import ipaddress
from urllib.parse import parse_qs, urlsplit

from django import forms

from .models import (
    EventMergeSuggestion,
    IntelligenceEvent,
    IntelligenceSource,
    IntelligenceSubject,
    SourceItem,
    SubjectKnowledgeIdentity,
    normalize_knowledge_author_name,
)


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
    knowledge_author_names = forms.CharField(
        label="知识中心历史作者名称",
        required=False,
        widget=forms.Textarea(attrs={"rows": 3, "placeholder": "金渐成\n金不换"}),
        help_text="每行一个。用于把知识中心已有作者明确连接到这个关注对象，不进行模糊匹配。",
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
        self.family = kwargs.pop("family", None)
        super().__init__(*args, **kwargs)
        if not self.is_bound and self.instance and self.instance.pk:
            self.initial["aliases"] = "\n".join(self.instance.aliases or [])
            if self.family is not None:
                self.initial["knowledge_author_names"] = "\n".join(
                    self.instance.knowledge_identities.filter(
                        family=self.family,
                        is_active=True,
                    ).values_list("author_name", flat=True)
                )

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

    def clean_knowledge_author_names(self):
        values = re.split(r"[\n,，、]+", self.cleaned_data["knowledge_author_names"])
        names = []
        seen = set()
        for value in values:
            name = " ".join(value.strip().split())
            normalized = normalize_knowledge_author_name(name)
            if name and normalized not in seen:
                names.append(name)
                seen.add(normalized)
        if self.family is None or not seen:
            return names
        conflicts = SubjectKnowledgeIdentity.objects.filter(
            family=self.family,
            normalized_author_name__in=seen,
        )
        if self.instance and self.instance.pk:
            conflicts = conflicts.exclude(subject=self.instance)
        conflict = conflicts.select_related("subject").first()
        if conflict:
            raise forms.ValidationError(
                f"“{conflict.author_name}”已连接到{conflict.subject.display_name}，请先核对人物身份。"
            )
        return names

    def save_knowledge_identities(self, *, subject, user):
        if self.family is None:
            return
        selected = {
            normalize_knowledge_author_name(name): name
            for name in self.cleaned_data.get("knowledge_author_names", [])
        }
        existing = {
            identity.normalized_author_name: identity
            for identity in SubjectKnowledgeIdentity.objects.filter(
                family=self.family,
                subject=subject,
            )
        }
        for normalized, name in selected.items():
            identity = existing.get(normalized)
            if identity is None:
                SubjectKnowledgeIdentity.objects.create(
                    family=self.family,
                    subject=subject,
                    author_name=name,
                    normalized_author_name=normalized,
                    created_by=user,
                    updated_by=user,
                )
            else:
                identity.author_name = name
                identity.is_active = True
                identity.updated_by = user
                identity.save(
                    update_fields=[
                        "author_name",
                        "normalized_author_name",
                        "is_active",
                        "updated_by",
                        "updated_at",
                    ]
                )
        for normalized, identity in existing.items():
            if normalized not in selected and identity.is_active:
                identity.is_active = False
                identity.updated_by = user
                identity.save(update_fields=["is_active", "updated_by", "updated_at"])


class IntelligenceSourceForm(StyledFormMixin, forms.ModelForm):
    article_fetch_policy = forms.ChoiceField(
        label="公开网页正文",
        choices=IntelligenceSource.ARTICLE_FETCH_POLICY_CHOICES,
        required=False,
        help_text="启用后只提取少量公开证据段落；不登录、不绕过付费墙，也不保存完整正文。",
    )

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
            "article_fetch_policy",
        ]
        help_texts = {
            "subject": "可选，用于指定默认归属；同一信源仍可关联多个主题。",
            "topics": "可多选人物、机构、行业、技术、政策或证券主题。",
            "adapter_key": "RSS 与 YouTube 官方频道已可采集；YouTube 只读取标题、简介等元数据，不下载视频。",
            "external_id": "YouTube 请填写以 UC 开头的 24 位频道 ID；RSS 可留空。",
            "transport_weight": "同等级来源中，官网可高于官方社交账号。",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            self.initial.setdefault("article_fetch_policy", self.instance.article_fetch_policy)
        else:
            self.initial.setdefault(
                "article_fetch_policy", IntelligenceSource.ARTICLE_FETCH_METADATA_ONLY
            )

    def clean(self):
        cleaned_data = super().clean()
        if not cleaned_data.get("subject") and not cleaned_data.get("topics"):
            self.add_error("topics", "请至少关联一个关注主题。")
        adapter_key = cleaned_data.get("adapter_key")
        source_type = cleaned_data.get("source_type")
        url = (cleaned_data.get("url") or "").strip()
        external_id = (cleaned_data.get("external_id") or "").strip()
        article_fetch_policy = (
            cleaned_data.get("article_fetch_policy")
            or IntelligenceSource.ARTICLE_FETCH_METADATA_ONLY
        )
        cleaned_data["article_fetch_policy"] = article_fetch_policy
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
        if (
            article_fetch_policy == IntelligenceSource.ARTICLE_FETCH_PUBLIC_HTML
            and adapter_key != IntelligenceSource.ADAPTER_RSS
        ):
            self.add_error("article_fetch_policy", "公开网页证据提取目前只适用于 RSS / Atom 信源。")
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
        extra_data = dict(self.instance.extra_data or {})
        extra_data["article_fetch_policy"] = self.cleaned_data["article_fetch_policy"]
        self.instance.extra_data = extra_data
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


class EventMergeConfirmForm(StyledFormMixin, forms.Form):
    canonical_event = forms.ModelChoiceField(
        label="保留哪一条事件",
        queryset=IntelligenceEvent.objects.none(),
        help_text="保留事件的标题、摘要和当前人工状态继续作为事件卡主体；另一条只隐藏，不会删除。",
    )
    primary_source_item = forms.ModelChoiceField(
        label="采用哪个主来源",
        queryset=SourceItem.objects.none(),
        help_text="系统优先推荐官方、监管、直接来源以及内容更完整的条目；其他来源仍作为证据保留。",
    )

    def __init__(self, *args, suggestion, **kwargs):
        super().__init__(*args, **kwargs)
        if not isinstance(suggestion, EventMergeSuggestion):
            raise TypeError("suggestion must be EventMergeSuggestion")
        self.suggestion = suggestion
        event_ids = [suggestion.left_event_id, suggestion.right_event_id]
        self.fields["canonical_event"].queryset = IntelligenceEvent.objects.filter(
            family=suggestion.family,
            pk__in=event_ids,
            merged_into__isnull=True,
        ).order_by("pk")
        source_ids = suggestion.left_event.evidence_links.values_list(
            "source_item_id", flat=True
        ).union(
            suggestion.right_event.evidence_links.values_list("source_item_id", flat=True)
        )
        self.fields["primary_source_item"].queryset = SourceItem.objects.filter(
            pk__in=source_ids
        ).select_related("source").order_by(
            "source__source_tier",
            "source__source_group",
            "pk",
        )
        self.fields["canonical_event"].initial = suggestion.recommended_event_id
        self.fields["primary_source_item"].initial = suggestion.recommended_primary_source_id

    def clean(self):
        cleaned_data = super().clean()
        canonical = cleaned_data.get("canonical_event")
        if canonical:
            pair_ids = {self.suggestion.left_event_id, self.suggestion.right_event_id}
            if canonical.pk not in pair_ids:
                self.add_error("canonical_event", "保留事件必须来自当前建议。")
        return cleaned_data
