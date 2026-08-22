import unicodedata
import uuid
from datetime import timedelta

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.utils.text import slugify

from family_core.models import Family, FamilyMember, TimestampedModel


def normalize_knowledge_author_name(value):
    return " ".join(
        unicodedata.normalize("NFKC", str(value or "")).strip().split()
    ).casefold()


class IntelligenceSubject(TimestampedModel):
    TYPE_PERSON = "person"
    TYPE_ORGANIZATION = "organization"
    TYPE_INDUSTRY = "industry"
    TYPE_TECHNOLOGY = "technology"
    TYPE_POLICY = "policy"
    TYPE_SECURITY = "security"
    TYPE_TOPIC = "topic"
    TYPE_CHOICES = [
        (TYPE_PERSON, "人物"),
        (TYPE_ORGANIZATION, "机构"),
        (TYPE_INDUSTRY, "行业"),
        (TYPE_TECHNOLOGY, "技术"),
        (TYPE_POLICY, "政策"),
        (TYPE_SECURITY, "证券"),
        (TYPE_TOPIC, "通用主题"),
    ]

    CATEGORY_TECH_LEADER = "tech_leader"
    CATEGORY_INVESTOR = "investor"
    CATEGORY_POLICY_LEADER = "policy_leader"
    CATEGORY_ORGANIZATION = "organization"
    CATEGORY_INDUSTRY = "industry"
    CATEGORY_TECHNOLOGY = "technology"
    CATEGORY_POLICY = "policy"
    CATEGORY_SECURITY = "security"
    CATEGORY_OTHER = "other"
    CATEGORY_CHOICES = [
        (CATEGORY_TECH_LEADER, "科技领袖"),
        (CATEGORY_INVESTOR, "投资人"),
        (CATEGORY_POLICY_LEADER, "政策人物"),
        (CATEGORY_ORGANIZATION, "机构"),
        (CATEGORY_INDUSTRY, "行业主题"),
        (CATEGORY_TECHNOLOGY, "技术主题"),
        (CATEGORY_POLICY, "政策主题"),
        (CATEGORY_SECURITY, "证券主题"),
        (CATEGORY_OTHER, "其他"),
    ]

    subject_type = models.CharField("对象类型", max_length=20, choices=TYPE_CHOICES)
    canonical_name = models.CharField("规范名称", max_length=200, unique=True)
    display_name = models.CharField("显示名称", max_length=200)
    slug = models.SlugField("稳定标识", max_length=220, unique=True, blank=True)
    aliases = models.JSONField("别名", default=list, blank=True)
    category = models.CharField("类别", max_length=30, choices=CATEGORY_CHOICES)
    profile_summary = models.TextField("简介", blank=True)
    avatar_url = models.URLField("头像链接", max_length=1000, blank=True)
    importance_level = models.PositiveSmallIntegerField(
        "重要性等级",
        default=3,
        validators=[MinValueValidator(1), MaxValueValidator(5)],
    )
    is_active = models.BooleanField("启用", default=True)
    extra_data = models.JSONField("扩展字段", default=dict, blank=True)

    class Meta:
        verbose_name = "情报关注主题"
        verbose_name_plural = "情报关注主题"
        ordering = ["-importance_level", "display_name", "pk"]
        indexes = [
            models.Index(fields=["subject_type", "category", "is_active"]),
        ]

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.canonical_name) or f"subject-{uuid.uuid4().hex[:12]}"
        super().save(*args, **kwargs)

    def __str__(self):
        return self.display_name


class SubjectRelation(TimestampedModel):
    TYPE_LEADERSHIP = "leadership"
    TYPE_FOUNDER = "founder"
    TYPE_INVESTOR = "investor"
    TYPE_AFFILIATION = "affiliation"
    TYPE_OTHER = "other"
    TYPE_CHOICES = [
        (TYPE_LEADERSHIP, "任职"),
        (TYPE_FOUNDER, "创办"),
        (TYPE_INVESTOR, "投资"),
        (TYPE_AFFILIATION, "关联"),
        (TYPE_OTHER, "其他"),
    ]

    from_subject = models.ForeignKey(
        IntelligenceSubject,
        verbose_name="起始对象",
        on_delete=models.CASCADE,
        related_name="outgoing_relations",
    )
    to_subject = models.ForeignKey(
        IntelligenceSubject,
        verbose_name="目标对象",
        on_delete=models.CASCADE,
        related_name="incoming_relations",
    )
    relation_type = models.CharField("关系类型", max_length=30, choices=TYPE_CHOICES)
    valid_from = models.DateField("开始日期", null=True, blank=True)
    valid_to = models.DateField("结束日期", null=True, blank=True)
    evidence_note = models.TextField("依据说明", blank=True)

    class Meta:
        verbose_name = "关注对象关系"
        verbose_name_plural = "关注对象关系"
        constraints = [
            models.CheckConstraint(
                condition=~Q(from_subject=models.F("to_subject")),
                name="intelligence_relation_subjects_differ",
            ),
            models.UniqueConstraint(
                fields=["from_subject", "to_subject", "relation_type"],
                name="unique_intelligence_subject_relation",
            ),
        ]

    def __str__(self):
        return f"{self.from_subject} - {self.get_relation_type_display()} - {self.to_subject}"


class IntelligenceSource(TimestampedModel):
    TYPE_OFFICIAL_SITE = "official_site"
    TYPE_RSS = "rss"
    TYPE_X = "x"
    TYPE_YOUTUBE = "youtube"
    TYPE_SEC = "sec"
    TYPE_MEDIA = "media"
    TYPE_MANUAL = "manual"
    TYPE_CHOICES = [
        (TYPE_OFFICIAL_SITE, "官方网站"),
        (TYPE_RSS, "RSS / Atom"),
        (TYPE_X, "X"),
        (TYPE_YOUTUBE, "YouTube"),
        (TYPE_SEC, "SEC EDGAR"),
        (TYPE_MEDIA, "媒体"),
        (TYPE_MANUAL, "人工录入"),
    ]

    GROUP_OFFICIAL = "official"
    GROUP_EXPERT = "expert"
    GROUP_INSTITUTION = "institution"
    GROUP_MEDIA = "media"
    GROUP_SOCIAL = "social"
    GROUP_REGULATORY = "regulatory"
    GROUP_OTHER = "other"
    GROUP_CHOICES = [
        (GROUP_OFFICIAL, "官方网站与官方账号"),
        (GROUP_EXPERT, "人物博客与访谈"),
        (GROUP_INSTITUTION, "公司与研究机构"),
        (GROUP_MEDIA, "可信媒体"),
        (GROUP_SOCIAL, "社交与视频平台"),
        (GROUP_REGULATORY, "监管与正式披露"),
        (GROUP_OTHER, "其他"),
    ]

    ADAPTER_MANUAL = "manual"
    ADAPTER_RSS = "rss"
    ADAPTER_YOUTUBE = "youtube"
    ADAPTER_SEC = "sec"
    ADAPTER_X = "x"
    ADAPTER_WEB = "web"
    ADAPTER_CHOICES = [
        (ADAPTER_MANUAL, "人工录入"),
        (ADAPTER_RSS, "RSS / Atom"),
        (ADAPTER_YOUTUBE, "YouTube 官方频道（仅元数据）"),
        (ADAPTER_SEC, "SEC EDGAR（待后续阶段）"),
        (ADAPTER_X, "X API（待凭证确认）"),
        (ADAPTER_WEB, "公开网页（待后续阶段）"),
    ]

    ARTICLE_FETCH_METADATA_ONLY = "metadata_only"
    ARTICLE_FETCH_PUBLIC_HTML = "public_html"
    ARTICLE_FETCH_POLICY_CHOICES = [
        (ARTICLE_FETCH_METADATA_ONLY, "只使用订阅元数据"),
        (ARTICLE_FETCH_PUBLIC_HTML, "提取公开网页证据摘录"),
    ]

    TIER_A = "A"
    TIER_B = "B"
    TIER_C = "C"
    TIER_D = "D"
    TIER_CHOICES = [
        (TIER_A, "A - 官方一手"),
        (TIER_B, "B - 直接采访/演讲"),
        (TIER_C, "C - 可信二手报道"),
        (TIER_D, "D - 发现线索"),
    ]

    subject = models.ForeignKey(
        IntelligenceSubject,
        verbose_name="主要关联主题",
        on_delete=models.SET_NULL,
        related_name="primary_sources",
        null=True,
        blank=True,
    )
    topics = models.ManyToManyField(
        IntelligenceSubject,
        verbose_name="关联主题",
        related_name="sources",
        blank=True,
    )
    source_type = models.CharField("来源类型", max_length=30, choices=TYPE_CHOICES)
    source_group = models.CharField("信源类别", max_length=30, choices=GROUP_CHOICES, default=GROUP_OTHER)
    adapter_key = models.SlugField("适配器代码", max_length=50, choices=ADAPTER_CHOICES, default=ADAPTER_MANUAL)
    name = models.CharField("来源名称", max_length=200)
    url = models.URLField("来源入口", max_length=1000, blank=True)
    external_id = models.CharField("平台外部 ID", max_length=300, blank=True)
    source_tier = models.CharField("来源等级", max_length=1, choices=TIER_CHOICES, default=TIER_C)
    transport_weight = models.PositiveSmallIntegerField(
        "载体权重",
        default=100,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        help_text="同一可信度下用于区分官网、官方社交账号等载体的信噪比。",
    )
    poll_interval_minutes = models.PositiveIntegerField("建议采集间隔（分钟）", default=60)
    cursor = models.JSONField("增量游标", default=dict, blank=True)
    last_attempt_at = models.DateTimeField("最近尝试时间", null=True, blank=True)
    last_success_at = models.DateTimeField("最近成功时间", null=True, blank=True)
    consecutive_failures = models.PositiveIntegerField("连续失败次数", default=0)
    last_error_summary = models.CharField("最近错误摘要", max_length=500, blank=True)
    is_active = models.BooleanField("启用", default=True)
    extra_data = models.JSONField("扩展字段", default=dict, blank=True)

    class Meta:
        verbose_name = "情报信息源"
        verbose_name_plural = "情报信息源"
        ordering = ["source_tier", "source_group", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["subject", "name"],
                name="unique_intelligence_source_name_per_subject",
            ),
            models.UniqueConstraint(
                fields=["adapter_key", "url"],
                condition=~Q(url="") & ~Q(adapter_key="manual"),
                name="unique_automatic_intelligence_source_url",
            ),
        ]
        indexes = [
            models.Index(fields=["source_type", "source_tier", "is_active"]),
        ]

    def __str__(self):
        return self.name

    @property
    def health_status(self):
        if not self.is_active:
            return "disabled"
        if self.consecutive_failures >= 3:
            return "error"
        if self.consecutive_failures:
            return "warning"
        return "healthy"

    @property
    def health_status_label(self):
        return {
            "disabled": "已停用",
            "error": "异常",
            "warning": "需注意",
            "healthy": "正常",
        }[self.health_status]

    @property
    def is_automatic(self):
        return self.adapter_key in {self.ADAPTER_RSS, self.ADAPTER_YOUTUBE}

    @property
    def article_fetch_policy(self):
        return (self.extra_data or {}).get(
            "article_fetch_policy", self.ARTICLE_FETCH_METADATA_ONLY
        )

    @property
    def article_fetch_enabled(self):
        return (
            self.adapter_key == self.ADAPTER_RSS
            and self.article_fetch_policy == self.ARTICLE_FETCH_PUBLIC_HTML
        )

    @property
    def is_due(self):
        if not self.is_active or self.adapter_key == self.ADAPTER_MANUAL:
            return False
        reference_at = self.last_attempt_at if self.consecutive_failures else self.last_success_at
        if reference_at is None:
            return True
        return reference_at <= timezone.now() - timedelta(minutes=self.poll_interval_minutes)


class SourceItem(TimestampedModel):
    DEPTH_TITLE = "title"
    DEPTH_DESCRIPTION = "description"
    DEPTH_PUBLIC_ARTICLE = "public_article"
    DEPTH_OFFICIAL_ARTICLE = "official_article"
    DEPTH_TRANSCRIPT = "transcript"
    DEPTH_MANUAL = "manual"
    DEPTH_CHOICES = [
        (DEPTH_TITLE, "仅标题"),
        (DEPTH_DESCRIPTION, "标题与简介"),
        (DEPTH_PUBLIC_ARTICLE, "公开网页证据摘录"),
        (DEPTH_OFFICIAL_ARTICLE, "官方文章正文"),
        (DEPTH_TRANSCRIPT, "完整字幕/文字稿"),
        (DEPTH_MANUAL, "人工核查"),
    ]

    STATUS_PENDING = "pending"
    STATUS_NORMALIZED = "normalized"
    STATUS_CLASSIFIED = "classified"
    STATUS_SCORED = "scored"
    STATUS_CLUSTERED = "clustered"
    STATUS_ANALYZED = "analyzed"
    STATUS_PUBLISHED = "published"
    STATUS_NOISE = "noise"
    STATUS_FAILED = "failed"
    STATUS_IGNORED = "ignored"
    STATUS_CHOICES = [
        (STATUS_PENDING, "待处理"),
        (STATUS_NORMALIZED, "已标准化"),
        (STATUS_CLASSIFIED, "已分类"),
        (STATUS_SCORED, "已评分"),
        (STATUS_CLUSTERED, "已聚类"),
        (STATUS_ANALYZED, "已分析"),
        (STATUS_PUBLISHED, "已发布"),
        (STATUS_NOISE, "噪音箱"),
        (STATUS_FAILED, "处理失败"),
        (STATUS_IGNORED, "已忽略"),
    ]

    ARTICLE_NOT_REQUESTED = "not_requested"
    ARTICLE_EXTRACTED = "extracted"
    ARTICLE_METADATA_ONLY = "metadata_only"
    ARTICLE_BLOCKED = "blocked"
    ARTICLE_FAILED = "failed"
    ARTICLE_FETCH_CHOICES = [
        (ARTICLE_NOT_REQUESTED, "未请求"),
        (ARTICLE_EXTRACTED, "已提取公开证据"),
        (ARTICLE_METADATA_ONLY, "仅保留元数据"),
        (ARTICLE_BLOCKED, "访问受限"),
        (ARTICLE_FAILED, "提取失败"),
    ]

    source = models.ForeignKey(
        IntelligenceSource,
        verbose_name="信息源",
        on_delete=models.PROTECT,
        related_name="items",
    )
    external_id = models.CharField("平台条目 ID", max_length=300, blank=True)
    canonical_url = models.URLField("原文链接", max_length=1000, blank=True)
    title = models.CharField("原始标题", max_length=500)
    author_name = models.CharField("发布者", max_length=200, blank=True)
    published_at = models.DateTimeField("发布时间", null=True, blank=True)
    fetched_at = models.DateTimeField("采集时间", default=timezone.now)
    language = models.CharField("原文语言", max_length=20, blank=True)
    excerpt = models.TextField("短摘录", blank=True)
    content_hash = models.CharField("内容指纹", max_length=64, blank=True, db_index=True)
    raw_metadata = models.JSONField("来源元数据", default=dict, blank=True)
    content_depth = models.CharField(
        "内容深度",
        max_length=30,
        choices=DEPTH_CHOICES,
        default=DEPTH_TITLE,
    )
    article_evidence = models.TextField(
        "公开网页证据摘录",
        blank=True,
        help_text="只保存自动整理所需的少量公开段落，不保存完整版权正文。",
    )
    article_content_hash = models.CharField(
        "公开网页内容指纹", max_length=64, blank=True, db_index=True
    )
    article_fetch_status = models.CharField(
        "网页提取状态",
        max_length=20,
        choices=ARTICLE_FETCH_CHOICES,
        default=ARTICLE_NOT_REQUESTED,
    )
    article_fetch_reason = models.CharField("网页提取说明", max_length=500, blank=True)
    article_fetched_at = models.DateTimeField("网页提取时间", null=True, blank=True)
    article_extraction_version = models.CharField("网页提取器版本", max_length=50, blank=True)
    matched_subjects = models.ManyToManyField(
        IntelligenceSubject,
        verbose_name="命中主题",
        related_name="matched_source_items",
        blank=True,
    )
    classification_labels = models.JSONField("分类标签", default=list, blank=True)
    relevance_score = models.PositiveSmallIntegerField(
        "相关性分数",
        default=0,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
    )
    processing_reason = models.CharField("处理说明", max_length=500, blank=True)
    processed_at = models.DateTimeField("处理完成时间", null=True, blank=True)
    processing_status = models.CharField(
        "处理状态",
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_PENDING,
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="创建人",
        on_delete=models.SET_NULL,
        related_name="created_intelligence_source_items",
        null=True,
        blank=True,
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="更新人",
        on_delete=models.SET_NULL,
        related_name="updated_intelligence_source_items",
        null=True,
        blank=True,
    )

    class Meta:
        verbose_name = "情报来源条目"
        verbose_name_plural = "情报来源条目"
        ordering = ["-published_at", "-fetched_at", "-pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["source", "external_id"],
                condition=~Q(external_id=""),
                name="unique_intelligence_source_external_item",
            ),
        ]
        indexes = [
            models.Index(fields=["source", "published_at"]),
            models.Index(fields=["processing_status", "fetched_at"]),
        ]

    def __str__(self):
        return self.title

class IntelligenceEvent(TimestampedModel):
    CHANNEL_PEOPLE = "people"

    TYPE_STATEMENT = "statement"
    TYPE_INTERVIEW = "interview"
    TYPE_INVESTMENT = "investment"
    TYPE_BUSINESS = "business"
    TYPE_ORGANIZATION = "organization"
    TYPE_POLICY = "policy"
    TYPE_OTHER = "other"
    TYPE_CHOICES = [
        (TYPE_STATEMENT, "发言与观点"),
        (TYPE_INTERVIEW, "采访与演讲"),
        (TYPE_INVESTMENT, "投资与持仓披露"),
        (TYPE_BUSINESS, "产品、经营与资本配置"),
        (TYPE_ORGANIZATION, "任职、组织与人物关系"),
        (TYPE_POLICY, "政策与公共事务"),
        (TYPE_OTHER, "其他重要动态"),
    ]

    PRECISION_EXACT = "exact"
    PRECISION_DATE = "date"
    PRECISION_ESTIMATED = "estimated"
    PRECISION_CHOICES = [
        (PRECISION_EXACT, "精确时间"),
        (PRECISION_DATE, "仅日期"),
        (PRECISION_ESTIMATED, "估计时间"),
    ]

    CHANGE_NEW = "new"
    CHANGE_CONTINUED = "continued"
    CHANGE_STRENGTHENED = "strengthened"
    CHANGE_WEAKENED = "weakened"
    CHANGE_REVERSED = "reversed"
    CHANGE_UNKNOWN = "unknown"
    CHANGE_CHOICES = [
        (CHANGE_NEW, "新动向"),
        (CHANGE_CONTINUED, "延续"),
        (CHANGE_STRENGTHENED, "增强"),
        (CHANGE_WEAKENED, "弱化"),
        (CHANGE_REVERSED, "转向"),
        (CHANGE_UNKNOWN, "无法判断"),
    ]

    REVIEW_PUBLISHED = "published"
    REVIEW_AI_PUBLISHED = "ai_published"
    REVIEW_PENDING = "pending"
    REVIEW_REVIEWED = "reviewed"
    REVIEW_IGNORED = "ignored"
    REVIEW_CHOICES = [
        (REVIEW_PUBLISHED, "已发布"),
        (REVIEW_AI_PUBLISHED, "AI 自动发布（未人工复核）"),
        (REVIEW_PENDING, "待复核"),
        (REVIEW_REVIEWED, "已复核"),
        (REVIEW_IGNORED, "已忽略"),
    ]

    SELECTION_SELECTED = "selected"
    SELECTION_FEED = "feed"
    SELECTION_REVIEW = "review"
    SELECTION_NOISE = "noise"
    SELECTION_CHOICES = [
        (SELECTION_SELECTED, "今日精选"),
        (SELECTION_FEED, "全部动态"),
        (SELECTION_REVIEW, "待复核"),
        (SELECTION_NOISE, "噪音箱"),
    ]

    SCORE_ORIGIN_MANUAL = "manual"
    SCORE_ORIGIN_RULES = "rules"
    SCORE_ORIGIN_AI = "ai"
    SCORE_ORIGIN_CHOICES = [
        (SCORE_ORIGIN_MANUAL, "人工特征 + 代码评分"),
        (SCORE_ORIGIN_RULES, "规则特征 + 代码评分"),
        (SCORE_ORIGIN_AI, "AI 特征 + 代码评分"),
    ]

    family = models.ForeignKey(
        Family,
        verbose_name="所属家庭",
        on_delete=models.CASCADE,
        related_name="intelligence_events",
    )
    channel = models.CharField("频道", max_length=30, default=CHANNEL_PEOPLE)
    event_type = models.CharField("事件类型", max_length=30, choices=TYPE_CHOICES)
    title = models.CharField("事件标题", max_length=500)
    occurred_at = models.DateTimeField("事件时间")
    occurred_precision = models.CharField(
        "时间精度",
        max_length=20,
        choices=PRECISION_CHOICES,
        default=PRECISION_EXACT,
    )
    summary = models.TextField("事实摘要")
    why_it_matters = models.TextField("为什么重要", blank=True)
    relevance_score = models.PositiveSmallIntegerField(
        "相关性",
        default=50,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
    )
    impact_score = models.PositiveSmallIntegerField(
        "影响程度",
        default=50,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
    )
    novelty_score = models.PositiveSmallIntegerField(
        "新颖性",
        default=50,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
    )
    actionability_score = models.PositiveSmallIntegerField(
        "投资参考价值",
        default=50,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
    )
    timeliness_score = models.PositiveSmallIntegerField(
        "时效性",
        default=50,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
    )
    importance_score = models.PositiveSmallIntegerField(
        "重要性分数",
        default=50,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
    )
    confidence_score = models.PositiveSmallIntegerField(
        "置信度",
        default=50,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
    )
    change_type = models.CharField(
        "变化信号",
        max_length=20,
        choices=CHANGE_CHOICES,
        default=CHANGE_UNKNOWN,
    )
    review_status = models.CharField(
        "复核状态",
        max_length=20,
        choices=REVIEW_CHOICES,
        default=REVIEW_PUBLISHED,
    )
    selection_status = models.CharField(
        "展示分层",
        max_length=20,
        choices=SELECTION_CHOICES,
        default=SELECTION_FEED,
    )
    scoring_policy_version = models.CharField("评分策略版本", max_length=50, default="people-v1")
    scoring_breakdown = models.JSONField("评分明细", default=dict, blank=True)
    score_origin = models.CharField(
        "评分来源",
        max_length=20,
        choices=SCORE_ORIGIN_CHOICES,
        default=SCORE_ORIGIN_MANUAL,
    )
    cluster_key = models.CharField("事件簇标识", max_length=100, blank=True, db_index=True)
    primary_source_item = models.ForeignKey(
        SourceItem,
        verbose_name="主来源条目",
        on_delete=models.SET_NULL,
        related_name="primary_for_events",
        null=True,
        blank=True,
    )
    merged_into = models.ForeignKey(
        "self",
        verbose_name="已合并到事件",
        on_delete=models.PROTECT,
        related_name="merged_duplicates",
        null=True,
        blank=True,
    )
    first_seen_at = models.DateTimeField("首次发现时间", default=timezone.now)
    last_seen_at = models.DateTimeField("最近发现时间", default=timezone.now)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="创建人",
        on_delete=models.SET_NULL,
        related_name="created_intelligence_events",
        null=True,
        blank=True,
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="更新人",
        on_delete=models.SET_NULL,
        related_name="updated_intelligence_events",
        null=True,
        blank=True,
    )
    subjects = models.ManyToManyField(
        IntelligenceSubject,
        through="EventSubject",
        related_name="intelligence_events",
    )

    class Meta:
        verbose_name = "情报事件"
        verbose_name_plural = "情报事件"
        ordering = ["-occurred_at", "-importance_score", "-pk"]
        indexes = [
            models.Index(fields=["family", "channel", "review_status", "occurred_at"]),
            models.Index(fields=["family", "selection_status", "occurred_at"]),
            models.Index(fields=["event_type", "importance_score"]),
        ]

    def __str__(self):
        return self.title

    @property
    def current_ai_analysis(self):
        prefetched = getattr(self, "_current_ai_analyses", None)
        if prefetched is not None:
            return prefetched[0] if prefetched else None
        return self.analyses.filter(
            is_current=True,
            status=EventAnalysis.STATUS_SUCCESS,
        ).first()

    @property
    def display_summary(self):
        analysis = self.current_ai_analysis
        if analysis and self.review_status in {self.REVIEW_PENDING, self.REVIEW_AI_PUBLISHED}:
            return analysis.result_json.get("summary") or self.summary
        return self.summary


class EventSubject(models.Model):
    ROLE_SPEAKER = "speaker"
    ROLE_SUBJECT = "subject"
    ROLE_INVESTOR = "investor"
    ROLE_EXECUTIVE = "executive"
    ROLE_MENTIONED = "mentioned"
    ROLE_AFFECTED_ORGANIZATION = "affected_organization"
    ROLE_CHOICES = [
        (ROLE_SPEAKER, "发言者"),
        (ROLE_SUBJECT, "事件主体"),
        (ROLE_INVESTOR, "投资者"),
        (ROLE_EXECUTIVE, "经营者"),
        (ROLE_MENTIONED, "被提及"),
        (ROLE_AFFECTED_ORGANIZATION, "受影响机构"),
    ]

    event = models.ForeignKey(
        IntelligenceEvent,
        verbose_name="情报事件",
        on_delete=models.CASCADE,
        related_name="subject_links",
    )
    subject = models.ForeignKey(
        IntelligenceSubject,
        verbose_name="关注对象",
        on_delete=models.PROTECT,
        related_name="event_links",
    )
    role = models.CharField("事件角色", max_length=30, choices=ROLE_CHOICES, default=ROLE_SUBJECT)
    confidence_score = models.PositiveSmallIntegerField(
        "关联置信度",
        default=100,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
    )
    is_primary = models.BooleanField("主要对象", default=False)

    class Meta:
        verbose_name = "事件关注对象"
        verbose_name_plural = "事件关注对象"
        constraints = [
            models.UniqueConstraint(
                fields=["event", "subject", "role"],
                name="unique_intelligence_event_subject_role",
            ),
        ]

    def __str__(self):
        return f"{self.event} - {self.subject}"


class EventEvidence(TimestampedModel):
    TYPE_FACT = "fact"
    TYPE_OPINION = "opinion"
    TYPE_CONTEXT = "context"
    TYPE_CHOICES = [
        (TYPE_FACT, "事实证据"),
        (TYPE_OPINION, "观点证据"),
        (TYPE_CONTEXT, "背景材料"),
    ]

    event = models.ForeignKey(
        IntelligenceEvent,
        verbose_name="情报事件",
        on_delete=models.CASCADE,
        related_name="evidence_links",
    )
    source_item = models.ForeignKey(
        SourceItem,
        verbose_name="来源条目",
        on_delete=models.PROTECT,
        related_name="event_evidence_links",
    )
    evidence_type = models.CharField("证据类型", max_length=20, choices=TYPE_CHOICES, default=TYPE_FACT)
    excerpt = models.TextField("证据摘录", blank=True)
    claim_ref = models.CharField("支持的陈述 ID", max_length=100, blank=True)
    source_quality_score = models.PositiveSmallIntegerField(
        "来源质量分",
        default=50,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
    )
    is_primary = models.BooleanField("主要证据", default=False)

    class Meta:
        verbose_name = "事件证据"
        verbose_name_plural = "事件证据"
        constraints = [
            models.UniqueConstraint(
                fields=["event", "source_item"],
                name="unique_intelligence_event_source_item",
            ),
        ]

    def __str__(self):
        return f"{self.event} - {self.source_item}"


class EventAnalysis(TimestampedModel):
    STATUS_PENDING = "pending"
    STATUS_SUCCESS = "success"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_PENDING, "处理中"),
        (STATUS_SUCCESS, "成功"),
        (STATUS_FAILED, "失败"),
    ]

    event = models.ForeignKey(
        IntelligenceEvent,
        verbose_name="情报事件",
        on_delete=models.CASCADE,
        related_name="analyses",
    )
    provider = models.ForeignKey(
        "ai_analysis.AiProvider",
        verbose_name="AI 服务商",
        on_delete=models.SET_NULL,
        related_name="intelligence_event_analyses",
        null=True,
        blank=True,
    )
    analysis_request = models.OneToOneField(
        "ai_analysis.AiAnalysisRequest",
        verbose_name="AI 请求审计",
        on_delete=models.SET_NULL,
        related_name="intelligence_event_analysis",
        null=True,
        blank=True,
    )
    model_name = models.CharField("实际模型", max_length=200, blank=True)
    prompt_version = models.CharField("提示词版本", max_length=100)
    schema_version = models.CharField("结构版本", max_length=100)
    input_fingerprint = models.CharField("输入指纹", max_length=64, db_index=True)
    input_snapshot = models.JSONField("输入审计快照", default=dict, blank=True)
    result_json = models.JSONField("结构化结果", default=dict, blank=True)
    status = models.CharField(
        "状态",
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_PENDING,
    )
    error_message = models.TextField("错误摘要", blank=True)
    tokens_used = models.PositiveIntegerField("Token 用量", null=True, blank=True)
    cost_estimate = models.DecimalField(
        "费用估算",
        max_digits=12,
        decimal_places=6,
        null=True,
        blank=True,
    )
    is_current = models.BooleanField("当前采用版本", default=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="发起人",
        on_delete=models.SET_NULL,
        related_name="created_intelligence_event_analyses",
        null=True,
        blank=True,
    )

    class Meta:
        verbose_name = "情报事件 AI 分析"
        verbose_name_plural = "情报事件 AI 分析"
        ordering = ["-created_at", "-pk"]
        indexes = [
            models.Index(fields=["event", "status", "created_at"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["event"],
                condition=Q(is_current=True),
                name="unique_current_intelligence_event_analysis",
            ),
        ]

    def __str__(self):
        return f"{self.event} - {self.model_name or 'AI'} - {self.get_status_display()}"


class EventMergeSuggestion(TimestampedModel):
    STATUS_PENDING = "pending"
    STATUS_ACCEPTED = "accepted"
    STATUS_REJECTED = "rejected"
    STATUS_STALE = "stale"
    STATUS_CHOICES = [
        (STATUS_PENDING, "等待复核"),
        (STATUS_ACCEPTED, "已接受"),
        (STATUS_REJECTED, "已拒绝"),
        (STATUS_STALE, "已失效"),
    ]

    BAND_BATCH = "batch"
    BAND_REVIEW = "review"
    BAND_CHOICES = [
        (BAND_BATCH, "建议批量聚合"),
        (BAND_REVIEW, "需要单项确认"),
    ]

    family = models.ForeignKey(
        Family,
        verbose_name="所属家庭",
        on_delete=models.CASCADE,
        related_name="intelligence_merge_suggestions",
    )
    left_event = models.ForeignKey(
        IntelligenceEvent,
        verbose_name="较小 ID 事件",
        on_delete=models.PROTECT,
        related_name="left_merge_suggestions",
    )
    right_event = models.ForeignKey(
        IntelligenceEvent,
        verbose_name="较大 ID 事件",
        on_delete=models.PROTECT,
        related_name="right_merge_suggestions",
    )
    recommended_event = models.ForeignKey(
        IntelligenceEvent,
        verbose_name="建议保留事件",
        on_delete=models.PROTECT,
        related_name="recommended_merge_suggestions",
    )
    recommended_primary_source = models.ForeignKey(
        SourceItem,
        verbose_name="建议主来源",
        on_delete=models.PROTECT,
        related_name="recommended_event_merge_suggestions",
        null=True,
        blank=True,
    )
    score = models.PositiveSmallIntegerField(
        "同事件置信分",
        validators=[MinValueValidator(0), MaxValueValidator(100)],
    )
    decision_band = models.CharField(
        "处理分层",
        max_length=20,
        choices=BAND_CHOICES,
        default=BAND_REVIEW,
    )
    policy_version = models.CharField("建议策略版本", max_length=100)
    reason = models.JSONField("建议依据", default=dict, blank=True)
    auto_merge_eligible = models.BooleanField("达到未来自动聚合门槛", default=False)
    requires_individual_review = models.BooleanField("必须单项确认", default=True)
    status = models.CharField(
        "建议状态",
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_PENDING,
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="复核人",
        on_delete=models.SET_NULL,
        related_name="reviewed_intelligence_merge_suggestions",
        null=True,
        blank=True,
    )
    reviewed_at = models.DateTimeField("复核时间", null=True, blank=True)

    class Meta:
        verbose_name = "同一事件建议"
        verbose_name_plural = "同一事件建议"
        ordering = ["-score", "created_at", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["left_event", "right_event", "policy_version"],
                name="unique_intelligence_merge_suggestion_pair_policy",
            ),
        ]
        indexes = [
            models.Index(fields=["family", "status", "decision_band", "score"]),
        ]

    def __str__(self):
        return f"{self.left_event} ↔ {self.right_event} ({self.score})"


class EventMergeRecord(TimestampedModel):
    STATUS_ACTIVE = "active"
    STATUS_REVERTED = "reverted"
    STATUS_CHOICES = [
        (STATUS_ACTIVE, "已合并"),
        (STATUS_REVERTED, "已拆分"),
    ]

    family = models.ForeignKey(
        Family,
        verbose_name="所属家庭",
        on_delete=models.CASCADE,
        related_name="intelligence_event_merges",
    )
    canonical_event = models.ForeignKey(
        IntelligenceEvent,
        verbose_name="保留事件",
        on_delete=models.PROTECT,
        related_name="canonical_merge_records",
    )
    duplicate_event = models.ForeignKey(
        IntelligenceEvent,
        verbose_name="并入事件",
        on_delete=models.PROTECT,
        related_name="duplicate_merge_records",
    )
    suggestion = models.OneToOneField(
        EventMergeSuggestion,
        verbose_name="采用建议",
        on_delete=models.SET_NULL,
        related_name="merge_record",
        null=True,
        blank=True,
    )
    status = models.CharField(
        "合并状态",
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_ACTIVE,
    )
    snapshot = models.JSONField("可逆操作快照", default=dict, blank=True)
    merged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="合并人",
        on_delete=models.SET_NULL,
        related_name="merged_intelligence_events",
        null=True,
        blank=True,
    )
    merged_at = models.DateTimeField("合并时间", default=timezone.now)
    reverted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="拆分人",
        on_delete=models.SET_NULL,
        related_name="reverted_intelligence_event_merges",
        null=True,
        blank=True,
    )
    reverted_at = models.DateTimeField("拆分时间", null=True, blank=True)

    class Meta:
        verbose_name = "事件合并记录"
        verbose_name_plural = "事件合并记录"
        ordering = ["-merged_at", "-pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["duplicate_event"],
                condition=Q(status="active"),
                name="unique_active_intelligence_merge_duplicate",
            ),
        ]
        indexes = [
            models.Index(fields=["family", "status", "merged_at"]),
        ]

    def __str__(self):
        return f"{self.duplicate_event} → {self.canonical_event}"


class SubjectFollow(TimestampedModel):
    family = models.ForeignKey(
        Family,
        verbose_name="所属家庭",
        on_delete=models.CASCADE,
        related_name="intelligence_subject_follows",
    )
    subject = models.ForeignKey(
        IntelligenceSubject,
        verbose_name="关注对象",
        on_delete=models.CASCADE,
        related_name="family_follows",
    )
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="添加人",
        on_delete=models.SET_NULL,
        related_name="added_intelligence_subject_follows",
        null=True,
        blank=True,
    )
    priority = models.PositiveSmallIntegerField(
        "关注优先级",
        default=3,
        validators=[MinValueValidator(1), MaxValueValidator(5)],
    )
    is_muted = models.BooleanField("静音", default=False)
    is_active = models.BooleanField("正在关注", default=True)

    class Meta:
        verbose_name = "家庭关注对象"
        verbose_name_plural = "家庭关注对象"
        constraints = [
            models.UniqueConstraint(
                fields=["family", "subject"],
                name="unique_family_intelligence_subject_follow",
            ),
        ]
        indexes = [models.Index(fields=["family", "is_active", "is_muted"])]

    def __str__(self):
        return f"{self.family} - {self.subject}"


class SubjectKnowledgeIdentity(TimestampedModel):
    family = models.ForeignKey(
        Family,
        verbose_name="所属家庭",
        on_delete=models.CASCADE,
        related_name="intelligence_knowledge_identities",
    )
    subject = models.ForeignKey(
        IntelligenceSubject,
        verbose_name="关注对象",
        on_delete=models.CASCADE,
        related_name="knowledge_identities",
    )
    author_name = models.CharField("知识库作者名称", max_length=300)
    normalized_author_name = models.CharField("规范作者名称", max_length=300)
    is_active = models.BooleanField("是否启用", default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="创建人",
        on_delete=models.SET_NULL,
        related_name="created_intelligence_knowledge_identities",
        null=True,
        blank=True,
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="更新人",
        on_delete=models.SET_NULL,
        related_name="updated_intelligence_knowledge_identities",
        null=True,
        blank=True,
    )

    class Meta:
        verbose_name = "人物知识身份映射"
        verbose_name_plural = "人物知识身份映射"
        ordering = ["author_name", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["family", "normalized_author_name"],
                name="unique_intelligence_knowledge_author_per_family",
            )
        ]
        indexes = [models.Index(fields=["family", "subject", "is_active"])]

    def save(self, *args, **kwargs):
        self.author_name = " ".join(str(self.author_name or "").strip().split())
        self.normalized_author_name = normalize_knowledge_author_name(self.author_name)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.subject} ↔ {self.author_name}"


class EventUserState(TimestampedModel):
    member = models.ForeignKey(
        FamilyMember,
        verbose_name="家庭成员",
        on_delete=models.CASCADE,
        related_name="intelligence_event_states",
    )
    event = models.ForeignKey(
        IntelligenceEvent,
        verbose_name="情报事件",
        on_delete=models.CASCADE,
        related_name="user_states",
    )
    read_at = models.DateTimeField("已读时间", null=True, blank=True)
    bookmarked_at = models.DateTimeField("收藏时间", null=True, blank=True)

    class Meta:
        verbose_name = "事件用户状态"
        verbose_name_plural = "事件用户状态"
        constraints = [
            models.UniqueConstraint(
                fields=["member", "event"],
                name="unique_member_intelligence_event_state",
            ),
        ]

    def __str__(self):
        return f"{self.member} - {self.event}"


class EventKnowledgeArchive(TimestampedModel):
    MODE_ARCHIVE = "archive"
    MODE_ORGANIZE = "organize"
    MODE_CHOICES = [
        (MODE_ARCHIVE, "归档"),
        (MODE_ORGANIZE, "归档并加入待整理"),
    ]

    event = models.OneToOneField(
        IntelligenceEvent,
        verbose_name="情报事件",
        on_delete=models.PROTECT,
        related_name="knowledge_archive",
    )
    document = models.OneToOneField(
        "knowledge.KnowledgeDocument",
        verbose_name="知识文档",
        on_delete=models.PROTECT,
        related_name="intelligence_archive",
    )
    archive_mode = models.CharField(
        "归档方式",
        max_length=20,
        choices=MODE_CHOICES,
        default=MODE_ARCHIVE,
    )
    archived_by = models.ForeignKey(
        FamilyMember,
        verbose_name="首次归档成员",
        on_delete=models.SET_NULL,
        related_name="intelligence_knowledge_archives",
        null=True,
        blank=True,
    )
    last_updated_by = models.ForeignKey(
        FamilyMember,
        verbose_name="最近调整成员",
        on_delete=models.SET_NULL,
        related_name="updated_intelligence_knowledge_archives",
        null=True,
        blank=True,
    )

    class Meta:
        verbose_name = "情报知识归档"
        verbose_name_plural = "情报知识归档"
        ordering = ["-created_at", "-pk"]

    def __str__(self):
        return f"{self.event} → {self.document}"


class IntelligenceDigest(TimestampedModel):
    POLICY_VERSION = "daily-brief-v1"

    family = models.ForeignKey(
        Family,
        verbose_name="所属家庭",
        on_delete=models.CASCADE,
        related_name="intelligence_digests",
    )
    digest_date = models.DateField("简报日期")
    title = models.CharField("标题", max_length=200)
    window_start = models.DateTimeField("窗口开始")
    window_end = models.DateTimeField("窗口结束")
    policy_version = models.CharField("选取策略版本", max_length=50, default=POLICY_VERSION)
    input_fingerprint = models.CharField("输入指纹", max_length=64, db_index=True)
    provider_names = models.JSONField("采用模型", default=list, blank=True)
    analysis_count = models.PositiveIntegerField("采用分析数量", default=0)
    tokens_used = models.PositiveIntegerField("采用分析 Token 合计", default=0)
    cost_estimate = models.DecimalField(
        "采用分析费用估算",
        max_digits=12,
        decimal_places=6,
        default=0,
    )
    generated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="生成人",
        on_delete=models.SET_NULL,
        related_name="generated_intelligence_digests",
        null=True,
        blank=True,
    )
    generated_at = models.DateTimeField("生成时间", default=timezone.now)

    class Meta:
        verbose_name = "AI 情报简报"
        verbose_name_plural = "AI 情报简报"
        ordering = ["-digest_date", "-pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["family", "digest_date"],
                name="unique_family_intelligence_digest_date",
            ),
        ]
        indexes = [models.Index(fields=["family", "digest_date"])]

    def __str__(self):
        return self.title


class IntelligenceDigestItem(models.Model):
    BUCKET_IMPORTANT = "important"
    BUCKET_FOLLOW_UP = "follow_up"
    BUCKET_REVIEW = "review"
    BUCKET_CHOICES = [
        (BUCKET_IMPORTANT, "今日重要"),
        (BUCKET_FOLLOW_UP, "值得跟进"),
        (BUCKET_REVIEW, "待确认"),
    ]

    digest = models.ForeignKey(
        IntelligenceDigest,
        verbose_name="所属简报",
        on_delete=models.CASCADE,
        related_name="items",
    )
    event = models.ForeignKey(
        IntelligenceEvent,
        verbose_name="情报事件",
        on_delete=models.PROTECT,
        related_name="digest_items",
    )
    analysis = models.ForeignKey(
        EventAnalysis,
        verbose_name="采用的 AI 分析",
        on_delete=models.SET_NULL,
        related_name="digest_items",
        null=True,
        blank=True,
    )
    bucket = models.CharField("简报分组", max_length=20, choices=BUCKET_CHOICES)
    position = models.PositiveSmallIntegerField("组内顺序")
    selection_reason = models.CharField("入选理由", max_length=500)
    title_snapshot = models.CharField("标题快照", max_length=500)
    summary_snapshot = models.TextField("摘要快照")
    why_it_matters_snapshot = models.TextField("重要性说明快照", blank=True)
    subject_names = models.JSONField("关注对象快照", default=list, blank=True)
    source_name = models.CharField("主来源快照", max_length=200, blank=True)
    source_url = models.URLField("主来源链接快照", max_length=1000, blank=True)
    occurred_at = models.DateTimeField("事件时间快照")
    importance_score = models.PositiveSmallIntegerField("重要性快照")
    confidence_score = models.PositiveSmallIntegerField("置信度快照")
    evidence_refs = models.JSONField("证据引用快照", default=list, blank=True)
    model_name_snapshot = models.CharField("采用模型快照", max_length=200, blank=True)
    tokens_used_snapshot = models.PositiveIntegerField("Token 用量快照", default=0)
    cost_estimate_snapshot = models.DecimalField(
        "费用估算快照",
        max_digits=12,
        decimal_places=6,
        default=0,
    )

    class Meta:
        verbose_name = "AI 情报简报条目"
        verbose_name_plural = "AI 情报简报条目"
        ordering = ["bucket", "position", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["digest", "event"],
                name="unique_intelligence_digest_event",
            ),
            models.UniqueConstraint(
                fields=["digest", "bucket", "position"],
                name="unique_intelligence_digest_bucket_position",
            ),
        ]

    def __str__(self):
        return f"{self.digest} · {self.get_bucket_display()} · {self.title_snapshot}"


class CollectionRun(models.Model):
    KIND_COLLECTION = "collection"
    KIND_PROCESSING = "processing"
    KIND_DIGEST = "digest"
    KIND_AUTOMATION = "automation"
    KIND_MANUAL = "manual"
    KIND_CHOICES = [
        (KIND_COLLECTION, "来源采集"),
        (KIND_PROCESSING, "条目处理"),
        (KIND_DIGEST, "简报生成"),
        (KIND_AUTOMATION, "自动情报循环"),
        (KIND_MANUAL, "人工录入"),
    ]

    STATUS_RUNNING = "running"
    STATUS_SUCCESS = "success"
    STATUS_PARTIAL = "partial"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_RUNNING, "执行中"),
        (STATUS_SUCCESS, "成功"),
        (STATUS_PARTIAL, "部分成功"),
        (STATUS_FAILED, "失败"),
    ]

    family = models.ForeignKey(
        Family,
        verbose_name="所属家庭",
        on_delete=models.CASCADE,
        related_name="intelligence_collection_runs",
        null=True,
        blank=True,
    )
    run_kind = models.CharField("运行类型", max_length=20, choices=KIND_CHOICES)
    status = models.CharField("状态", max_length=20, choices=STATUS_CHOICES, default=STATUS_RUNNING)
    started_at = models.DateTimeField("开始时间", default=timezone.now)
    finished_at = models.DateTimeField("结束时间", null=True, blank=True)
    parameters = models.JSONField("运行参数", default=dict, blank=True)
    discovered_count = models.PositiveIntegerField("发现数量", default=0)
    created_count = models.PositiveIntegerField("新增数量", default=0)
    updated_count = models.PositiveIntegerField("更新数量", default=0)
    ignored_count = models.PositiveIntegerField("忽略数量", default=0)
    normalized_count = models.PositiveIntegerField("标准化数量", default=0)
    classified_count = models.PositiveIntegerField("分类数量", default=0)
    noise_count = models.PositiveIntegerField("噪音数量", default=0)
    clustered_count = models.PositiveIntegerField("聚类数量", default=0)
    selected_count = models.PositiveIntegerField("精选数量", default=0)
    review_count = models.PositiveIntegerField("待复核数量", default=0)
    failed_count = models.PositiveIntegerField("失败数量", default=0)
    error_summary = models.TextField("错误摘要", blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="发起人",
        on_delete=models.SET_NULL,
        related_name="intelligence_collection_runs",
        null=True,
        blank=True,
    )

    class Meta:
        verbose_name = "情报运行记录"
        verbose_name_plural = "情报运行记录"
        ordering = ["-started_at", "-pk"]
        indexes = [models.Index(fields=["family", "run_kind", "status", "started_at"])]

    def __str__(self):
        return f"{self.get_run_kind_display()} - {self.started_at:%Y-%m-%d %H:%M}"


class CollectionRunItem(models.Model):
    run = models.ForeignKey(
        CollectionRun,
        verbose_name="运行记录",
        on_delete=models.CASCADE,
        related_name="source_results",
    )
    source = models.ForeignKey(
        IntelligenceSource,
        verbose_name="信息源",
        on_delete=models.SET_NULL,
        related_name="run_results",
        null=True,
        blank=True,
    )
    status = models.CharField("状态", max_length=20, choices=CollectionRun.STATUS_CHOICES)
    discovered_count = models.PositiveIntegerField("发现数量", default=0)
    created_count = models.PositiveIntegerField("新增数量", default=0)
    updated_count = models.PositiveIntegerField("更新数量", default=0)
    ignored_count = models.PositiveIntegerField("重复/未变化数量", default=0)
    noise_count = models.PositiveIntegerField("噪音数量", default=0)
    clustered_count = models.PositiveIntegerField("候选事件数量", default=0)
    failed_count = models.PositiveIntegerField("失败数量", default=0)
    cursor_before = models.JSONField("采集前游标", default=dict, blank=True)
    cursor_after = models.JSONField("采集后游标", default=dict, blank=True)
    error_summary = models.TextField("错误摘要", blank=True)

    class Meta:
        verbose_name = "情报来源运行明细"
        verbose_name_plural = "情报来源运行明细"
        ordering = ["pk"]

    def __str__(self):
        return f"{self.run} - {self.source or '全局'}"
