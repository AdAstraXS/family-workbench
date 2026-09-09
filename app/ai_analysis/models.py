from django.db import models
from django.db.models import Q

from family_core.models import Family, FamilyMember, TimestampedModel


class AiProvider(TimestampedModel):
    LOCATION_CLOUD = "cloud"
    LOCATION_LOCAL = "local"
    LOCATION_CHOICES = [
        (LOCATION_CLOUD, "云端"),
        (LOCATION_LOCAL, "本地"),
    ]

    name = models.CharField("服务商名称", max_length=100)
    provider_type = models.CharField("服务商类型", max_length=50)
    base_url = models.URLField("API 地址", max_length=500, blank=True)
    model_name = models.CharField("默认模型", max_length=100, blank=True)
    execution_location = models.CharField(
        "运行位置",
        max_length=20,
        choices=LOCATION_CHOICES,
        default=LOCATION_CLOUD,
    )
    is_active = models.BooleanField("是否启用", default=True)
    extra_data = models.JSONField("扩展字段", default=dict, blank=True)

    class Meta:
        verbose_name = "AI 服务商"
        verbose_name_plural = "AI 服务商"

    def __str__(self):
        return f"{self.name} {self.model_name}".strip()


class AiConversation(TimestampedModel):
    SCOPE_PERSONAL = "personal"
    SCOPE_FAMILY = "family"
    SCOPE_CHOICES = [
        (SCOPE_PERSONAL, "我的财务"),
        (SCOPE_FAMILY, "全家财务"),
    ]

    family = models.ForeignKey(
        Family,
        verbose_name="所属家庭",
        on_delete=models.CASCADE,
        related_name="ai_conversations",
    )
    member = models.ForeignKey(
        FamilyMember,
        verbose_name="会话所有者",
        on_delete=models.CASCADE,
        related_name="ai_conversations",
    )
    title = models.CharField("标题", max_length=200, blank=True)
    financial_scope = models.CharField(
        "财务范围",
        max_length=20,
        choices=SCOPE_CHOICES,
        default=SCOPE_PERSONAL,
    )
    is_archived = models.BooleanField("已归档", default=False)

    class Meta:
        verbose_name = "全局 AI 会话"
        verbose_name_plural = "全局 AI 会话"
        indexes = [
            models.Index(
                fields=["family", "member", "is_archived", "-updated_at"],
                name="ai_conv_owner_state_idx",
            )
        ]

    def __str__(self):
        return self.title or f"会话 #{self.pk}"


class AiMemory(TimestampedModel):
    VISIBILITY_PERSONAL = "personal"
    VISIBILITY_FAMILY = "family"
    VISIBILITY_CHOICES = [
        (VISIBILITY_PERSONAL, "仅自己"),
        (VISIBILITY_FAMILY, "家庭共同记录"),
    ]
    STATUS_CANDIDATE = "candidate"
    STATUS_CONFIRMED = "confirmed"
    STATUS_REJECTED = "rejected"
    STATUS_SUPERSEDED = "superseded"
    STATUS_DELETED = "deleted"
    STATUS_CHOICES = [
        (STATUS_CANDIDATE, "待确认"),
        (STATUS_CONFIRMED, "已确认"),
        (STATUS_REJECTED, "已拒绝"),
        (STATUS_SUPERSEDED, "已被新版本替代"),
        (STATUS_DELETED, "已删除"),
    ]

    family = models.ForeignKey(
        Family, verbose_name="所属家庭", on_delete=models.CASCADE, related_name="ai_memories"
    )
    owner = models.ForeignKey(
        FamilyMember,
        verbose_name="个人记录所有者",
        on_delete=models.CASCADE,
        related_name="ai_memories",
        null=True,
        blank=True,
    )
    created_by = models.ForeignKey(
        FamilyMember,
        verbose_name="创建成员",
        on_delete=models.PROTECT,
        related_name="created_ai_memories",
    )
    confirmed_by = models.ForeignKey(
        FamilyMember,
        verbose_name="确认成员",
        on_delete=models.PROTECT,
        related_name="confirmed_ai_memories",
        null=True,
        blank=True,
    )
    visibility = models.CharField(
        "可见范围", max_length=20, choices=VISIBILITY_CHOICES, default=VISIBILITY_PERSONAL
    )
    status = models.CharField(
        "状态", max_length=20, choices=STATUS_CHOICES, default=STATUS_CANDIDATE
    )
    content = models.TextField("内容")
    source_note = models.CharField("来源说明", max_length=300, blank=True)
    version = models.PositiveIntegerField("版本", default=1)
    supersedes = models.ForeignKey(
        "self",
        verbose_name="上一版本",
        on_delete=models.PROTECT,
        related_name="replacement_versions",
        null=True,
        blank=True,
    )
    confirmed_at = models.DateTimeField("确认时间", null=True, blank=True)
    deleted_at = models.DateTimeField("删除时间", null=True, blank=True)

    class Meta:
        verbose_name = "全局 AI 记忆"
        verbose_name_plural = "全局 AI 记忆"
        indexes = [
            models.Index(
                fields=["family", "owner", "visibility", "status"],
                name="ai_memory_access_idx",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(visibility="personal", owner__isnull=False)
                    | Q(visibility="family", owner__isnull=True)
                ),
                name="ai_memory_owner_matches_visibility",
            ),
        ]

    def __str__(self):
        return f"{self.get_visibility_display()} · v{self.version}"


class AiOutboundAuthorization(TimestampedModel):
    DATA_CONVERSATION = "conversation"
    DATA_MEMORY = "memory"
    DATA_KNOWLEDGE = "knowledge"
    DATA_FINANCIAL = "financial"
    DATA_TYPE_CHOICES = [
        (DATA_CONVERSATION, "对话"),
        (DATA_MEMORY, "记忆"),
        (DATA_KNOWLEDGE, "知识正文"),
        (DATA_FINANCIAL, "财务数据"),
    ]

    family = models.ForeignKey(
        Family,
        verbose_name="所属家庭",
        on_delete=models.CASCADE,
        related_name="ai_outbound_authorizations",
    )
    member = models.ForeignKey(
        FamilyMember,
        verbose_name="授权成员",
        on_delete=models.CASCADE,
        related_name="ai_outbound_authorizations",
    )
    provider = models.ForeignKey(
        AiProvider,
        verbose_name="服务商",
        on_delete=models.CASCADE,
        related_name="outbound_authorizations",
    )
    data_type = models.CharField("数据类型", max_length=30, choices=DATA_TYPE_CHOICES)
    is_allowed = models.BooleanField("允许发送", default=False)

    class Meta:
        verbose_name = "全局 AI 外发授权"
        verbose_name_plural = "全局 AI 外发授权"
        constraints = [
            models.UniqueConstraint(
                fields=["family", "member", "provider", "data_type"],
                name="unique_ai_outbound_authorization",
            )
        ]


class AiFamilyOutboundAuthorization(TimestampedModel):
    DATA_FINANCIAL = "financial"
    DATA_TYPE_CHOICES = [(DATA_FINANCIAL, "全家财务数据")]

    family = models.ForeignKey(
        Family,
        verbose_name="所属家庭",
        on_delete=models.CASCADE,
        related_name="ai_family_outbound_authorizations",
    )
    provider = models.ForeignKey(
        AiProvider,
        verbose_name="服务商",
        on_delete=models.CASCADE,
        related_name="family_outbound_authorizations",
    )
    data_type = models.CharField(
        "数据类型",
        max_length=30,
        choices=DATA_TYPE_CHOICES,
        default=DATA_FINANCIAL,
    )
    is_allowed = models.BooleanField("允许发送", default=False)
    changed_by = models.ForeignKey(
        FamilyMember,
        verbose_name="最近操作成员",
        on_delete=models.PROTECT,
        related_name="changed_ai_family_outbound_authorizations",
    )

    class Meta:
        verbose_name = "全局 AI 家庭外发授权"
        verbose_name_plural = "全局 AI 家庭外发授权"
        constraints = [
            models.UniqueConstraint(
                fields=["family", "provider", "data_type"],
                name="unique_ai_family_outbound_authorization",
            ),
            models.CheckConstraint(
                condition=Q(data_type="financial"),
                name="ai_family_outbound_financial_only",
            ),
        ]


class AiConversationMessage(models.Model):
    ROLE_USER = "user"
    ROLE_ASSISTANT = "assistant"
    ROLE_SUMMARY = "summary"
    ROLE_CHOICES = [
        (ROLE_USER, "成员"),
        (ROLE_ASSISTANT, "助手"),
        (ROLE_SUMMARY, "会话摘要"),
    ]

    conversation = models.ForeignKey(
        AiConversation,
        verbose_name="所属会话",
        on_delete=models.CASCADE,
        related_name="messages",
    )
    sequence = models.PositiveIntegerField("顺序")
    role = models.CharField("角色", max_length=20, choices=ROLE_CHOICES)
    content = models.TextField("内容")
    data_types = models.JSONField("包含的数据类型", default=list, blank=True)
    evidence_refs = models.JSONField("证据引用", default=list, blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        verbose_name = "全局 AI 会话消息"
        verbose_name_plural = "全局 AI 会话消息"
        ordering = ["sequence", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["conversation", "sequence"], name="unique_ai_message_sequence"
            )
        ]


class AiAnswerShare(TimestampedModel):
    STATUS_DRAFT = "draft"
    STATUS_ACTIVE = "active"
    STATUS_PAUSED = "paused"
    STATUS_WITHDRAWN = "withdrawn"
    STATUS_CHOICES = [
        (STATUS_DRAFT, "等待预览"),
        (STATUS_ACTIVE, "家庭可见"),
        (STATUS_PAUSED, "已暂停"),
        (STATUS_WITHDRAWN, "已撤回"),
    ]

    family = models.ForeignKey(
        Family,
        verbose_name="所属家庭",
        on_delete=models.CASCADE,
        related_name="ai_answer_shares",
    )
    owner = models.ForeignKey(
        FamilyMember,
        verbose_name="分享成员",
        on_delete=models.CASCADE,
        related_name="ai_answer_shares",
    )
    source_message = models.ForeignKey(
        AiConversationMessage,
        verbose_name="来源回答",
        on_delete=models.CASCADE,
        related_name="answer_shares",
    )
    title = models.CharField("分享标题", max_length=200, blank=True)
    status = models.CharField(
        "状态", max_length=20, choices=STATUS_CHOICES, default=STATUS_DRAFT
    )
    answer_text_snapshot = models.TextField("回答副本")
    evidence_snapshot = models.JSONField("依据副本", default=list, blank=True)
    source_message_hash = models.CharField("来源回答校验值", max_length=64)
    published_at = models.DateTimeField("发布时间", null=True, blank=True)
    paused_at = models.DateTimeField("暂停时间", null=True, blank=True)
    pause_reason = models.CharField("暂停原因", max_length=300, blank=True)
    withdrawn_at = models.DateTimeField("撤回时间", null=True, blank=True)

    class Meta:
        verbose_name = "全局 AI 单条回答分享"
        verbose_name_plural = "全局 AI 单条回答分享"
        ordering = ["-created_at", "-pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["source_message"],
                condition=Q(status__in=["draft", "active", "paused"]),
                name="unique_open_ai_answer_share",
            )
        ]

    def __str__(self):
        return self.title or f"回答分享 #{self.pk}"


class AiAnalysisRequest(TimestampedModel):
    STATUS_PENDING = "pending"
    STATUS_RUNNING = "running"
    STATUS_CANCEL_REQUESTED = "cancel_requested"
    STATUS_CANCELLED = "cancelled"
    STATUS_UNKNOWN = "unknown"
    STATUS_SUCCESS = "success"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_PENDING, "等待中"),
        (STATUS_RUNNING, "运行中"),
        (STATUS_CANCEL_REQUESTED, "正在停止"),
        (STATUS_CANCELLED, "已停止"),
        (STATUS_UNKNOWN, "结果未知"),
        (STATUS_SUCCESS, "成功"),
        (STATUS_FAILED, "失败"),
    ]

    family = models.ForeignKey(Family, verbose_name="所属家庭", on_delete=models.CASCADE, related_name="ai_requests")
    member = models.ForeignKey(FamilyMember, verbose_name="发起成员", on_delete=models.CASCADE, related_name="ai_requests")
    conversation = models.ForeignKey(
        AiConversation,
        verbose_name="全局 AI 会话",
        on_delete=models.SET_NULL,
        related_name="requests",
        null=True,
        blank=True,
    )
    provider = models.ForeignKey(AiProvider, verbose_name="AI 服务商", on_delete=models.SET_NULL, related_name="requests", null=True, blank=True)
    module = models.CharField("分析模块", max_length=50)
    analysis_type = models.CharField("分析类型", max_length=100, blank=True)
    scope = models.JSONField("分析范围", default=dict, blank=True)
    prompt = models.TextField("提示词")
    sanitized_input = models.JSONField("脱敏后的输入数据", default=dict, blank=True)
    status = models.CharField("状态", max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    error_message = models.TextField("错误信息", blank=True)
    idempotency_key = models.CharField("幂等键", max_length=100, blank=True)
    request_fingerprint = models.CharField("请求指纹", max_length=64, blank=True)
    execution_token = models.CharField("执行令牌", max_length=64, blank=True)
    started_at = models.DateTimeField("开始时间", null=True, blank=True)
    finished_at = models.DateTimeField("结束时间", null=True, blank=True)

    class Meta:
        verbose_name = "AI 分析请求"
        verbose_name_plural = "AI 分析请求"
        indexes = [
            models.Index(fields=["family", "member", "module", "created_at"]),
            models.Index(fields=["status"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["family", "member", "module", "idempotency_key"],
                condition=~Q(idempotency_key=""),
                name="unique_ai_request_idempotency_key",
            )
        ]

    def __str__(self):
        return f"{self.module} - {self.member} - {self.created_at:%Y-%m-%d %H:%M}"


class AiAnalysisResult(models.Model):
    request = models.OneToOneField(AiAnalysisRequest, verbose_name="请求", on_delete=models.CASCADE, related_name="result")
    result_text = models.TextField("分析结果", blank=True)
    result_json = models.JSONField("结构化结果", default=dict, blank=True)
    tokens_used = models.PositiveIntegerField("Token 用量", null=True, blank=True)
    cost_estimate = models.DecimalField("费用估算", max_digits=12, decimal_places=6, null=True, blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        verbose_name = "AI 分析结果"
        verbose_name_plural = "AI 分析结果"

    def __str__(self):
        return f"AI 结果 #{self.pk}"
