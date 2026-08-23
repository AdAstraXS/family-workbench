import uuid
import unicodedata
from pathlib import Path

from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.utils.text import get_valid_filename

from family_core.models import Family, FamilyMember, TimestampedModel

from .crypto import decrypt_json, encrypt_json
from .storage import protected_knowledge_storage


def normalize_taxonomy_name(value):
    """Return a stable comparison key without changing the displayed name."""
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    return " ".join(normalized.strip().split()).casefold()


def _safe_filename(filename, fallback):
    name = get_valid_filename(Path(filename or "").name)
    return name[:180] or fallback


def revision_raw_upload_to(instance, filename):
    return (
        f"families/{instance.document.family_id}/"
        f"sources/{instance.document.source_id}/"
        f"documents/{instance.document_id}/"
        f"revisions/{instance.revision_number}/raw/"
        f"{_safe_filename(filename, 'page.html')}"
    )


def asset_upload_to(instance, filename):
    return (
        f"families/{instance.revision.document.family_id}/"
        f"sources/{instance.revision.document.source_id}/"
        f"documents/{instance.revision.document_id}/"
        f"revisions/{instance.revision.revision_number}/assets/"
        f"{_safe_filename(filename, 'resource.bin')}"
    )


def import_package_upload_to(instance, filename):
    return (
        f"families/{instance.family_id}/imports/{instance.batch_key}/package/"
        f"{_safe_filename(filename, 'knowledge-import.zip')}"
    )


def artifact_original_upload_to(instance, filename):
    return (
        f"families/{instance.artifact.family_id}/artifacts/{instance.artifact_id}/"
        f"versions/{instance.version_number}/original/"
        f"{_safe_filename(filename, 'artifact.html')}"
    )


def artifact_rendered_upload_to(instance, filename):
    return (
        f"families/{instance.artifact.family_id}/artifacts/{instance.artifact_id}/"
        f"versions/{instance.version_number}/rendered/"
        f"{_safe_filename(filename, 'artifact.html')}"
    )


class KnowledgeVisibility(models.TextChoices):
    PRIVATE = "private", "仅自己"
    FAMILY = "family", "家庭可见"


class SourceConnection(TimestampedModel):
    PROVIDER_MICROSOFT = "microsoft"
    PROVIDER_CHOICES = [(PROVIDER_MICROSOFT, "Microsoft")]

    STATUS_ACTIVE = "active"
    STATUS_ERROR = "error"
    STATUS_DISCONNECTED = "disconnected"
    STATUS_CHOICES = [
        (STATUS_ACTIVE, "正常"),
        (STATUS_ERROR, "需要处理"),
        (STATUS_DISCONNECTED, "已断开"),
    ]

    family = models.ForeignKey(
        Family,
        verbose_name="所属家庭",
        on_delete=models.CASCADE,
        related_name="knowledge_connections",
    )
    member = models.ForeignKey(
        FamilyMember,
        verbose_name="授权成员",
        on_delete=models.CASCADE,
        related_name="knowledge_connections",
    )
    provider = models.CharField(
        "服务商",
        max_length=30,
        choices=PROVIDER_CHOICES,
        default=PROVIDER_MICROSOFT,
    )
    external_account_id = models.CharField("外部账户 ID", max_length=255, blank=True)
    account_display_name = models.CharField("账户显示名称", max_length=200, blank=True)
    account_email = models.CharField("账户邮箱", max_length=320, blank=True)
    encrypted_token_cache = models.TextField("加密令牌缓存", blank=True, editable=False)
    granted_scopes = models.JSONField("已授权范围", default=list, blank=True)
    available_notebooks = models.JSONField("可选笔记本缓存", default=list, blank=True)
    status = models.CharField(
        "连接状态",
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_DISCONNECTED,
    )
    last_used_at = models.DateTimeField("最近使用时间", null=True, blank=True)
    last_success_at = models.DateTimeField("最近成功时间", null=True, blank=True)
    last_error = models.TextField("最近错误摘要", blank=True)

    class Meta:
        verbose_name = "知识来源账户连接"
        verbose_name_plural = "知识来源账户连接"
        constraints = [
            models.UniqueConstraint(
                fields=["family", "member", "provider"],
                name="unique_knowledge_connection_per_member_provider",
            )
        ]
        indexes = [
            models.Index(fields=["family", "provider", "status"]),
            models.Index(fields=["member", "provider"]),
        ]

    def __str__(self):
        return f"{self.member} · {self.get_provider_display()}"

    def set_token_cache(self, serialized_cache):
        self.encrypted_token_cache = encrypt_json({"cache": serialized_cache})

    def get_token_cache(self):
        return decrypt_json(self.encrypted_token_cache).get("cache", "")

    def clear_token_cache(self):
        self.encrypted_token_cache = ""


class KnowledgeSource(TimestampedModel):
    KIND_ONENOTE = "onenote"
    KIND_INTERNAL_NOTES = "internal_notes"
    KIND_HTML_IMPORT = "html_import"
    KIND_MARKDOWN_IMPORT = "markdown_import"
    KIND_INTELLIGENCE = "intelligence"
    KIND_CHOICES = [
        (KIND_ONENOTE, "OneNote"),
        (KIND_INTERNAL_NOTES, "随手记"),
        (KIND_HTML_IMPORT, "HTML 历史资料"),
        (KIND_MARKDOWN_IMPORT, "Markdown 历史资料"),
        (KIND_INTELLIGENCE, "AI 情报归档"),
    ]

    ROUTE_KNOWLEDGE = "knowledge"
    ROUTE_ORGANIZE = "organize"
    ROUTE_ARCHIVE = "archive"
    ROUTE_CHOICES = [
        (ROUTE_KNOWLEDGE, "进入归档资料"),
        (ROUTE_ORGANIZE, "归档并加入待整理"),
        (ROUTE_ARCHIVE, "仅同步（不进入资料库）"),
    ]

    STATUS_ACTIVE = "active"
    STATUS_ERROR = "error"
    STATUS_DISCONNECTED = "disconnected"
    STATUS_CHOICES = [
        (STATUS_ACTIVE, "正常"),
        (STATUS_ERROR, "需要处理"),
        (STATUS_DISCONNECTED, "连接已断开"),
    ]

    family = models.ForeignKey(
        Family,
        verbose_name="所属家庭",
        on_delete=models.CASCADE,
        related_name="knowledge_sources",
    )
    owner = models.ForeignKey(
        FamilyMember,
        verbose_name="来源所有者",
        on_delete=models.SET_NULL,
        related_name="knowledge_sources",
        null=True,
        blank=True,
    )
    connection = models.ForeignKey(
        SourceConnection,
        verbose_name="账户连接",
        on_delete=models.SET_NULL,
        related_name="sources",
        null=True,
        blank=True,
    )
    key = models.CharField("稳定来源键", max_length=500)
    kind = models.CharField("来源类型", max_length=30, choices=KIND_CHOICES)
    name = models.CharField("来源名称", max_length=300)
    external_id = models.CharField("外部来源 ID", max_length=500, blank=True)
    source_url = models.URLField("来源链接", max_length=1000, blank=True)
    visibility = models.CharField(
        "默认可见范围",
        max_length=20,
        choices=KnowledgeVisibility.choices,
        default=KnowledgeVisibility.FAMILY,
    )
    allow_cloud_ai = models.BooleanField(
        "允许发送正文给已配置的云端 AI",
        default=False,
    )
    status = models.CharField(
        "来源状态",
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_ACTIVE,
    )
    is_enabled = models.BooleanField("是否启用", default=True)
    config = models.JSONField("非敏感配置", default=dict, blank=True)
    sync_cursor = models.JSONField("同步游标", default=dict, blank=True)
    last_sync_at = models.DateTimeField("最近同步时间", null=True, blank=True)
    last_reconciled_at = models.DateTimeField("最近完整对账时间", null=True, blank=True)
    last_error = models.TextField("最近错误摘要", blank=True)

    class Meta:
        verbose_name = "知识来源"
        verbose_name_plural = "知识来源"
        ordering = ["kind", "name", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["family", "key"],
                name="unique_knowledge_source_key_per_family",
            )
        ]
        indexes = [
            models.Index(fields=["family", "kind", "status"]),
            models.Index(fields=["owner", "kind"]),
        ]

    def __str__(self):
        return self.name

    @property
    def default_route(self):
        value = (self.config or {}).get("default_route")
        return value if value in dict(self.ROUTE_CHOICES) else self.ROUTE_KNOWLEDGE

    def route_for_section(self, section_id):
        routes = (self.config or {}).get("section_routes") or {}
        value = routes.get(str(section_id or ""), self.default_route)
        return value if value in dict(self.ROUTE_CHOICES) else self.default_route


class KnowledgeCategory(TimestampedModel):
    family = models.ForeignKey(
        Family,
        verbose_name="所属家庭",
        on_delete=models.CASCADE,
        related_name="knowledge_categories",
    )
    name = models.CharField("分类名称", max_length=100)
    normalized_name = models.CharField("规范名称", max_length=100)
    description = models.CharField("说明", max_length=300, blank=True)
    aliases = models.JSONField("历史名称与别名", default=list, blank=True)
    is_active = models.BooleanField("是否启用", default=True)
    merged_into = models.ForeignKey(
        "self",
        verbose_name="已合并到",
        on_delete=models.PROTECT,
        related_name="merged_categories",
        null=True,
        blank=True,
    )
    created_by = models.ForeignKey(
        FamilyMember,
        verbose_name="创建人",
        on_delete=models.SET_NULL,
        related_name="created_knowledge_categories",
        null=True,
        blank=True,
    )

    class Meta:
        verbose_name = "知识分类"
        verbose_name_plural = "知识分类"
        ordering = ["name", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["family", "normalized_name"],
                name="unique_knowledge_category_name_per_family",
            )
        ]
        indexes = [models.Index(fields=["family", "is_active", "name"])]

    def save(self, *args, **kwargs):
        self.name = " ".join(str(self.name or "").strip().split())
        self.normalized_name = normalize_taxonomy_name(self.name)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class KnowledgeTag(TimestampedModel):
    family = models.ForeignKey(
        Family,
        verbose_name="所属家庭",
        on_delete=models.CASCADE,
        related_name="knowledge_tags",
    )
    name = models.CharField("标签名称", max_length=30)
    normalized_name = models.CharField("规范名称", max_length=30)
    description = models.CharField("说明", max_length=300, blank=True)
    aliases = models.JSONField("历史名称与别名", default=list, blank=True)
    is_active = models.BooleanField("是否启用", default=True)
    merged_into = models.ForeignKey(
        "self",
        verbose_name="已合并到",
        on_delete=models.PROTECT,
        related_name="merged_tags",
        null=True,
        blank=True,
    )
    created_by = models.ForeignKey(
        FamilyMember,
        verbose_name="创建人",
        on_delete=models.SET_NULL,
        related_name="created_knowledge_tags",
        null=True,
        blank=True,
    )

    class Meta:
        verbose_name = "知识标签"
        verbose_name_plural = "知识标签"
        ordering = ["name", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["family", "normalized_name"],
                name="unique_knowledge_tag_name_per_family",
            )
        ]
        indexes = [models.Index(fields=["family", "is_active", "name"])]

    def save(self, *args, **kwargs):
        self.name = " ".join(str(self.name or "").strip().split())
        self.normalized_name = normalize_taxonomy_name(self.name)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class KnowledgeDocument(TimestampedModel):
    SYNC_AVAILABLE = "available"
    SYNC_ERROR = "error"
    SYNC_SOURCE_DELETED = "source_deleted"
    SYNC_CHOICES = [
        (SYNC_AVAILABLE, "来源可用"),
        (SYNC_ERROR, "同步异常"),
        (SYNC_SOURCE_DELETED, "来源已删除或不可访问"),
    ]

    CURATION_INBOX = "inbox"
    CURATION_NORMALIZED = "normalized"
    CURATION_PENDING_AI = "pending_ai"
    CURATION_PENDING_REVIEW = "pending_review"
    CURATION_CONFIRMED = "confirmed"
    CURATION_IGNORED = "ignored"
    CURATION_ARCHIVED = "archived"
    CURATION_CHOICES = [
        (CURATION_INBOX, "收件箱"),
        (CURATION_NORMALIZED, "已规范化"),
        (CURATION_PENDING_AI, "待 AI 处理"),
        (CURATION_PENDING_REVIEW, "待人工确认"),
        (CURATION_CONFIRMED, "已确认"),
        (CURATION_IGNORED, "已忽略"),
        (CURATION_ARCHIVED, "已归档"),
    ]

    KNOWLEDGE_PENDING = "pending"
    KNOWLEDGE_INCLUDED = "included"
    KNOWLEDGE_ARCHIVED = "archived"
    KNOWLEDGE_STATUS_CHOICES = [
        (KNOWLEDGE_PENDING, "待整理"),
        (KNOWLEDGE_INCLUDED, "归档资料"),
        (KNOWLEDGE_ARCHIVED, "仅同步"),
    ]

    LIBRARY_KNOWLEDGE = "knowledge"
    LIBRARY_ARCHIVE = "archive"
    LIBRARY_TIER_CHOICES = [
        (LIBRARY_KNOWLEDGE, "精选知识"),
        (LIBRARY_ARCHIVE, "仅归档"),
    ]

    family = models.ForeignKey(
        Family,
        verbose_name="所属家庭",
        on_delete=models.CASCADE,
        related_name="knowledge_documents",
    )
    source = models.ForeignKey(
        KnowledgeSource,
        verbose_name="知识来源",
        on_delete=models.PROTECT,
        related_name="documents",
    )
    owner = models.ForeignKey(
        FamilyMember,
        verbose_name="资料所有者",
        on_delete=models.SET_NULL,
        related_name="knowledge_documents",
        null=True,
        blank=True,
    )
    external_id = models.CharField("外部文档 ID", max_length=500)
    title = models.CharField("标题", max_length=500)
    author = models.CharField("作者", max_length=300, blank=True)
    section_name = models.CharField("分区", max_length=300, blank=True)
    hierarchy = models.JSONField("来源层级", default=dict, blank=True)
    source_url = models.URLField("原始链接", max_length=1000, blank=True)
    visibility = models.CharField(
        "可见范围",
        max_length=20,
        choices=KnowledgeVisibility.choices,
        default=KnowledgeVisibility.FAMILY,
    )
    sync_status = models.CharField(
        "同步状态",
        max_length=30,
        choices=SYNC_CHOICES,
        default=SYNC_AVAILABLE,
    )
    curation_status = models.CharField(
        "整理状态",
        max_length=30,
        choices=CURATION_CHOICES,
        default=CURATION_INBOX,
    )
    knowledge_status = models.CharField(
        "知识状态",
        max_length=20,
        choices=KNOWLEDGE_STATUS_CHOICES,
        default=KNOWLEDGE_INCLUDED,
    )
    content_created_at = models.DateTimeField("内容创建时间", null=True, blank=True)
    content_modified_at = models.DateTimeField("内容修改时间", null=True, blank=True)
    source_deleted_at = models.DateTimeField("来源删除识别时间", null=True, blank=True)
    confirmed_summary = models.TextField("已确认摘要", blank=True)
    category = models.CharField("已确认分类", max_length=100, blank=True)
    tags = models.JSONField("已确认标签", default=list, blank=True)
    library_tier = models.CharField(
        "精选状态",
        max_length=20,
        choices=LIBRARY_TIER_CHOICES,
        default=LIBRARY_ARCHIVE,
        db_index=True,
    )
    current_revision = models.ForeignKey(
        "KnowledgeRevision",
        verbose_name="当前内容版本",
        on_delete=models.SET_NULL,
        related_name="+",
        null=True,
        blank=True,
    )

    class Meta:
        verbose_name = "知识文档"
        verbose_name_plural = "知识文档"
        ordering = ["-content_modified_at", "-updated_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["source", "external_id"],
                name="unique_knowledge_document_per_source",
            )
        ]
        indexes = [
            models.Index(fields=["family", "visibility", "owner"]),
            models.Index(fields=["source", "sync_status"]),
            models.Index(fields=["family", "curation_status"]),
            models.Index(fields=["family", "knowledge_status"]),
            models.Index(fields=["family", "library_tier", "knowledge_status"]),
            models.Index(fields=["content_modified_at"]),
        ]

    def __str__(self):
        return self.title


class KnowledgeRevision(models.Model):
    document = models.ForeignKey(
        KnowledgeDocument,
        verbose_name="知识文档",
        on_delete=models.CASCADE,
        related_name="revisions",
    )
    revision_number = models.PositiveIntegerField("版本号")
    content_hash = models.CharField("内容哈希", max_length=64)
    normalized_hash = models.CharField("规范正文哈希", max_length=64, blank=True)
    raw_file = models.FileField(
        "原始内容",
        storage=protected_knowledge_storage,
        upload_to=revision_raw_upload_to,
        max_length=1000,
    )
    normalized_html = models.TextField("安全展示正文", blank=True)
    plain_text = models.TextField("纯文本正文", blank=True)
    converter_version = models.CharField("转换器版本", max_length=50, default="onenote-html-v1")
    source_modified_at = models.DateTimeField("来源修改时间", null=True, blank=True)
    created_at = models.DateTimeField("同步时间", auto_now_add=True)

    class Meta:
        verbose_name = "知识文档版本"
        verbose_name_plural = "知识文档版本"
        ordering = ["-revision_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["document", "revision_number"],
                name="unique_knowledge_revision_number",
            ),
            models.UniqueConstraint(
                fields=["document", "content_hash"],
                name="unique_knowledge_revision_content_hash",
            ),
        ]
        indexes = [
            models.Index(fields=["document", "created_at"]),
            models.Index(fields=["content_hash"]),
        ]

    def __str__(self):
        return f"{self.document} · v{self.revision_number}"


class KnowledgeAsset(models.Model):
    revision = models.ForeignKey(
        KnowledgeRevision,
        verbose_name="内容版本",
        on_delete=models.CASCADE,
        related_name="assets",
    )
    external_id = models.CharField("外部资源 ID", max_length=500)
    original_name = models.CharField("原始文件名", max_length=300, blank=True)
    source_path = models.CharField("原始相对路径", max_length=1000, blank=True)
    mime_type = models.CharField("MIME 类型", max_length=200)
    byte_size = models.PositiveBigIntegerField("文件大小")
    content_hash = models.CharField("文件哈希", max_length=64)
    is_image = models.BooleanField("是否为正文图片", default=False)
    file = models.FileField(
        "受保护文件",
        storage=protected_knowledge_storage,
        upload_to=asset_upload_to,
        max_length=1000,
    )
    created_at = models.DateTimeField("保存时间", auto_now_add=True)

    class Meta:
        verbose_name = "知识附件"
        verbose_name_plural = "知识附件"
        constraints = [
            models.UniqueConstraint(
                fields=["revision", "external_id"],
                name="unique_knowledge_asset_per_revision",
            )
        ]
        indexes = [
            models.Index(fields=["revision", "is_image"]),
            models.Index(fields=["content_hash"]),
        ]

    def __str__(self):
        return self.original_name or self.external_id


class KnowledgeProposalRun(models.Model):
    document = models.ForeignKey(
        KnowledgeDocument,
        verbose_name="知识文档",
        on_delete=models.CASCADE,
        related_name="proposal_runs",
    )
    revision = models.ForeignKey(
        KnowledgeRevision,
        verbose_name="对应内容版本",
        on_delete=models.CASCADE,
        related_name="proposal_runs",
    )
    sequence = models.PositiveIntegerField("整理轮次")
    requested_by = models.ForeignKey(
        FamilyMember,
        verbose_name="发起成员",
        on_delete=models.SET_NULL,
        related_name="knowledge_proposal_runs",
        null=True,
        blank=True,
    )
    analysis_request = models.OneToOneField(
        "ai_analysis.AiAnalysisRequest",
        verbose_name="AI 请求",
        on_delete=models.SET_NULL,
        related_name="knowledge_proposal_run",
        null=True,
        blank=True,
    )
    model_name = models.CharField("模型", max_length=200)
    prompt_version = models.CharField("提示词版本", max_length=50)
    content_hash = models.CharField("输入内容哈希", max_length=64)
    created_at = models.DateTimeField("生成时间", auto_now_add=True)

    class Meta:
        verbose_name = "知识 AI 整理轮次"
        verbose_name_plural = "知识 AI 整理轮次"
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["document", "sequence"],
                name="unique_knowledge_proposal_run_sequence",
            )
        ]
        indexes = [
            models.Index(fields=["document", "created_at"]),
            models.Index(fields=["revision", "created_at"]),
        ]

    def __str__(self):
        return f"{self.document} · 第 {self.sequence} 次 AI 整理"


class KnowledgeCurationRevision(models.Model):
    TYPE_MANUAL = "manual"
    TYPE_AI_CONFIRMED = "ai_confirmed"
    TYPE_LEGACY = "legacy"
    TYPE_CHOICES = [
        (TYPE_MANUAL, "人工整理"),
        (TYPE_AI_CONFIRMED, "确认 AI 建议"),
        (TYPE_LEGACY, "既有整理结果"),
    ]

    document = models.ForeignKey(
        KnowledgeDocument,
        verbose_name="知识文档",
        on_delete=models.CASCADE,
        related_name="curation_revisions",
    )
    sequence = models.PositiveIntegerField("整理版本")
    summary = models.TextField("正式摘要", blank=True)
    category = models.CharField("正式分类", max_length=100, blank=True)
    tags = models.JSONField("正式标签", default=list, blank=True)
    change_type = models.CharField("整理方式", max_length=30, choices=TYPE_CHOICES)
    changed_by = models.ForeignKey(
        FamilyMember,
        verbose_name="整理人",
        on_delete=models.SET_NULL,
        related_name="knowledge_curation_revisions",
        null=True,
        blank=True,
    )
    proposal_run = models.ForeignKey(
        KnowledgeProposalRun,
        verbose_name="采用的 AI 整理轮次",
        on_delete=models.SET_NULL,
        related_name="curation_revisions",
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField("保存时间", auto_now_add=True)

    class Meta:
        verbose_name = "知识正式整理版本"
        verbose_name_plural = "知识正式整理版本"
        ordering = ["-sequence", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["document", "sequence"],
                name="unique_knowledge_curation_revision_sequence",
            )
        ]

    def __str__(self):
        return f"{self.document} · 整理版本 {self.sequence}"


class KnowledgeProposal(models.Model):
    TYPE_SUMMARY = "summary"
    TYPE_TAGS = "tags"
    TYPE_CATEGORY = "category"
    TYPE_CHOICES = [
        (TYPE_SUMMARY, "摘要建议"),
        (TYPE_TAGS, "标签建议"),
        (TYPE_CATEGORY, "分类建议"),
    ]

    STATUS_PENDING = "pending"
    STATUS_ACCEPTED = "accepted"
    STATUS_REJECTED = "rejected"
    STATUS_STALE = "stale"
    STATUS_CHOICES = [
        (STATUS_PENDING, "待确认"),
        (STATUS_ACCEPTED, "已接受"),
        (STATUS_REJECTED, "已拒绝"),
        (STATUS_STALE, "内容已更新"),
    ]

    document = models.ForeignKey(
        KnowledgeDocument,
        verbose_name="知识文档",
        on_delete=models.CASCADE,
        related_name="proposals",
    )
    revision = models.ForeignKey(
        KnowledgeRevision,
        verbose_name="对应内容版本",
        on_delete=models.CASCADE,
        related_name="proposals",
    )
    run = models.ForeignKey(
        KnowledgeProposalRun,
        verbose_name="整理轮次",
        on_delete=models.CASCADE,
        related_name="proposals",
    )
    proposal_type = models.CharField("建议类型", max_length=30, choices=TYPE_CHOICES)
    suggested_value = models.JSONField("AI 建议", default=dict)
    human_value = models.JSONField("人工确认值", default=dict, blank=True)
    model_name = models.CharField("模型", max_length=200)
    prompt_version = models.CharField("提示词版本", max_length=50)
    content_hash = models.CharField("输入内容哈希", max_length=64)
    status = models.CharField(
        "确认状态",
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_PENDING,
    )
    confirmed_by = models.ForeignKey(
        FamilyMember,
        verbose_name="确认人",
        on_delete=models.SET_NULL,
        related_name="confirmed_knowledge_proposals",
        null=True,
        blank=True,
    )
    confirmed_at = models.DateTimeField("确认时间", null=True, blank=True)
    created_at = models.DateTimeField("生成时间", auto_now_add=True)

    class Meta:
        verbose_name = "知识整理建议"
        verbose_name_plural = "知识整理建议"
        ordering = ["created_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["run", "proposal_type"],
                name="unique_knowledge_proposal_type_per_run",
            )
        ]
        indexes = [
            models.Index(fields=["document", "status"]),
            models.Index(fields=["revision", "status"]),
        ]

    def __str__(self):
        return f"{self.document} · {self.get_proposal_type_display()}"


class KnowledgeJob(TimestampedModel):
    TYPE_SYNC_SOURCE = "sync_source"
    TYPE_GENERATE_PROPOSALS = "generate_proposals"
    TYPE_REBUILD_SEARCH = "rebuild_search"
    TYPE_PREVIEW_IMPORT = "preview_import"
    TYPE_IMPORT_BATCH = "import_batch"
    TYPE_ROLLBACK_IMPORT = "rollback_import"
    TYPE_CHOICES = [
        (TYPE_SYNC_SOURCE, "同步来源"),
        (TYPE_GENERATE_PROPOSALS, "生成 AI 整理建议"),
        (TYPE_REBUILD_SEARCH, "重建搜索索引"),
        (TYPE_PREVIEW_IMPORT, "检查导入资料"),
        (TYPE_IMPORT_BATCH, "执行资料导入"),
        (TYPE_ROLLBACK_IMPORT, "回滚导入批次"),
    ]

    STATUS_PENDING = "pending"
    STATUS_RUNNING = "running"
    STATUS_SUCCESS = "success"
    STATUS_PARTIAL = "partial"
    STATUS_FAILED = "failed"
    STATUS_SOURCE_UNAVAILABLE = "source_unavailable"
    STATUS_CANCEL_REQUESTED = "cancel_requested"
    STATUS_CANCELLED = "cancelled"
    STATUS_CHOICES = [
        (STATUS_PENDING, "排队中"),
        (STATUS_RUNNING, "运行中"),
        (STATUS_SUCCESS, "成功"),
        (STATUS_PARTIAL, "部分成功"),
        (STATUS_FAILED, "失败"),
        (STATUS_SOURCE_UNAVAILABLE, "来源不可访问"),
        (STATUS_CANCEL_REQUESTED, "正在取消"),
        (STATUS_CANCELLED, "已取消"),
    ]
    ACTIVE_STATUSES = (STATUS_PENDING, STATUS_RUNNING, STATUS_CANCEL_REQUESTED)

    family = models.ForeignKey(
        Family,
        verbose_name="所属家庭",
        on_delete=models.CASCADE,
        related_name="knowledge_jobs",
    )
    source = models.ForeignKey(
        KnowledgeSource,
        verbose_name="知识来源",
        on_delete=models.CASCADE,
        related_name="jobs",
        null=True,
        blank=True,
    )
    requested_by = models.ForeignKey(
        FamilyMember,
        verbose_name="发起成员",
        on_delete=models.SET_NULL,
        related_name="knowledge_jobs",
        null=True,
        blank=True,
    )
    job_type = models.CharField("任务类型", max_length=40, choices=TYPE_CHOICES)
    status = models.CharField(
        "任务状态",
        max_length=30,
        choices=STATUS_CHOICES,
        default=STATUS_PENDING,
    )
    parameters = models.JSONField("任务参数", default=dict, blank=True)
    cursor = models.JSONField("任务游标", default=dict, blank=True)
    total_count = models.PositiveIntegerField("总项数", default=0)
    success_count = models.PositiveIntegerField("新增数", default=0)
    updated_count = models.PositiveIntegerField("更新数", default=0)
    skipped_count = models.PositiveIntegerField("跳过数", default=0)
    failed_count = models.PositiveIntegerField("失败数", default=0)
    scheduled_at = models.DateTimeField("计划时间", default=timezone.now)
    started_at = models.DateTimeField("开始时间", null=True, blank=True)
    finished_at = models.DateTimeField("结束时间", null=True, blank=True)
    heartbeat_at = models.DateTimeField("最近心跳", null=True, blank=True)
    error_message = models.TextField("错误摘要", blank=True)
    result = models.JSONField("任务结果", default=dict, blank=True)

    class Meta:
        verbose_name = "知识后台任务"
        verbose_name_plural = "知识后台任务"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["source", "job_type"],
                condition=Q(status__in=["pending", "running", "cancel_requested"]),
                name="unique_active_knowledge_job_per_source_type",
            )
        ]
        indexes = [
            models.Index(fields=["status", "scheduled_at"]),
            models.Index(fields=["family", "created_at"]),
            models.Index(fields=["source", "job_type", "created_at"]),
        ]

    def __str__(self):
        return f"{self.get_job_type_display()} · {self.get_status_display()}"


class KnowledgeJobItem(models.Model):
    STATUS_SUCCESS = "success"
    STATUS_UPDATED = "updated"
    STATUS_SKIPPED = "skipped"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_SUCCESS, "新增"),
        (STATUS_UPDATED, "更新"),
        (STATUS_SKIPPED, "跳过"),
        (STATUS_FAILED, "失败"),
    ]

    job = models.ForeignKey(
        KnowledgeJob,
        verbose_name="所属任务",
        on_delete=models.CASCADE,
        related_name="items",
    )
    external_id = models.CharField("外部项目 ID", max_length=500)
    title = models.CharField("项目标题", max_length=500, blank=True)
    status = models.CharField("处理状态", max_length=20, choices=STATUS_CHOICES)
    error_message = models.TextField("错误", blank=True)
    details = models.JSONField("详情", default=dict, blank=True)
    created_at = models.DateTimeField("处理时间", auto_now_add=True)

    class Meta:
        verbose_name = "知识任务项目"
        verbose_name_plural = "知识任务项目"
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(
                fields=["job", "external_id"],
                name="unique_knowledge_job_item",
            )
        ]
        indexes = [models.Index(fields=["job", "status"])]

    def __str__(self):
        return self.title or self.external_id


class KnowledgeImportBatch(TimestampedModel):
    FORMAT_HTML = "html"
    FORMAT_MARKDOWN = "markdown"
    FORMAT_CHOICES = [
        (FORMAT_HTML, "HTML"),
        (FORMAT_MARKDOWN, "Markdown"),
    ]

    STATUS_UPLOADED = "uploaded"
    STATUS_PREVIEWING = "previewing"
    STATUS_PREVIEW_READY = "preview_ready"
    STATUS_IMPORTING = "importing"
    STATUS_COMPLETED = "completed"
    STATUS_PARTIAL = "partial"
    STATUS_FAILED = "failed"
    STATUS_ROLLING_BACK = "rolling_back"
    STATUS_ROLLED_BACK = "rolled_back"
    STATUS_CHOICES = [
        (STATUS_UPLOADED, "等待格式检查"),
        (STATUS_PREVIEWING, "正在格式检查"),
        (STATUS_PREVIEW_READY, "等待确认导入"),
        (STATUS_IMPORTING, "正在导入"),
        (STATUS_COMPLETED, "导入完成"),
        (STATUS_PARTIAL, "部分完成"),
        (STATUS_FAILED, "需要处理"),
        (STATUS_ROLLING_BACK, "正在回滚"),
        (STATUS_ROLLED_BACK, "已回滚"),
    ]

    batch_key = models.UUIDField("批次标识", default=uuid.uuid4, unique=True, editable=False)
    family = models.ForeignKey(
        Family,
        verbose_name="所属家庭",
        on_delete=models.CASCADE,
        related_name="knowledge_import_batches",
    )
    source = models.ForeignKey(
        KnowledgeSource,
        verbose_name="知识来源",
        on_delete=models.PROTECT,
        related_name="import_batches",
    )
    requested_by = models.ForeignKey(
        FamilyMember,
        verbose_name="发起成员",
        on_delete=models.SET_NULL,
        related_name="knowledge_import_batches",
        null=True,
        blank=True,
    )
    import_format = models.CharField("导入格式", max_length=20, choices=FORMAT_CHOICES)
    source_filename = models.CharField("上传文件名", max_length=300)
    source_sha256 = models.CharField("上传包校验值", max_length=64)
    package_file = models.FileField(
        "受保护的原始导入包",
        storage=protected_knowledge_storage,
        upload_to=import_package_upload_to,
        max_length=1000,
    )
    visibility = models.CharField(
        "默认可见范围",
        max_length=20,
        choices=KnowledgeVisibility.choices,
        default=KnowledgeVisibility.FAMILY,
    )
    person_name = models.CharField(
        "归属人物",
        max_length=300,
        blank=True,
        help_text="填写后统一作为本批次文章的作者；HTML 原始署名仍保留在逐篇记录中。",
    )
    category = models.CharField("批次默认分类", max_length=100, blank=True)
    status = models.CharField(
        "批次状态",
        max_length=30,
        choices=STATUS_CHOICES,
        default=STATUS_UPLOADED,
    )
    total_count = models.PositiveIntegerField("文件数", default=0)
    new_count = models.PositiveIntegerField("预计新增", default=0)
    update_count = models.PositiveIntegerField("预计更新", default=0)
    skipped_count = models.PositiveIntegerField("预计跳过", default=0)
    duplicate_count = models.PositiveIntegerField("强重复", default=0)
    error_count = models.PositiveIntegerField("错误数", default=0)
    asset_count = models.PositiveIntegerField("图片数", default=0)
    estimated_bytes = models.PositiveBigIntegerField("预计保存字节", default=0)
    previewed_at = models.DateTimeField("预览完成时间", null=True, blank=True)
    confirmed_at = models.DateTimeField("确认导入时间", null=True, blank=True)
    completed_at = models.DateTimeField("导入完成时间", null=True, blank=True)
    rolled_back_at = models.DateTimeField("回滚完成时间", null=True, blank=True)
    error_message = models.TextField("错误摘要", blank=True)
    result = models.JSONField("批次结果", default=dict, blank=True)

    class Meta:
        verbose_name = "知识导入批次"
        verbose_name_plural = "知识导入批次"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["family", "status", "created_at"]),
            models.Index(fields=["source", "created_at"]),
            models.Index(fields=["source_sha256"]),
        ]

    def __str__(self):
        return f"{self.source} · {self.source_filename}"


class KnowledgeImportItem(models.Model):
    ACTION_NEW = "new"
    ACTION_UPDATE = "update"
    ACTION_UNCHANGED = "unchanged"
    ACTION_DUPLICATE = "duplicate"
    ACTION_ERROR = "error"
    ACTION_CHOICES = [
        (ACTION_NEW, "新增"),
        (ACTION_UPDATE, "更新"),
        (ACTION_UNCHANGED, "内容未变化"),
        (ACTION_DUPLICATE, "强重复"),
        (ACTION_ERROR, "无法导入"),
    ]

    STATUS_PREVIEWED = "previewed"
    STATUS_IMPORTED = "imported"
    STATUS_SKIPPED = "skipped"
    STATUS_FAILED = "failed"
    STATUS_ROLLED_BACK = "rolled_back"
    STATUS_CHOICES = [
        (STATUS_PREVIEWED, "已检查"),
        (STATUS_IMPORTED, "已导入"),
        (STATUS_SKIPPED, "已跳过"),
        (STATUS_FAILED, "失败"),
        (STATUS_ROLLED_BACK, "已回滚"),
    ]

    batch = models.ForeignKey(
        KnowledgeImportBatch,
        verbose_name="导入批次",
        on_delete=models.CASCADE,
        related_name="items",
    )
    relative_path = models.CharField("原始相对路径", max_length=1000)
    external_id = models.CharField("稳定文档 ID", max_length=500, blank=True)
    title = models.CharField("标题", max_length=500, blank=True)
    author = models.CharField("作者", max_length=300, blank=True)
    source_url = models.URLField("原文链接", max_length=1000, blank=True)
    published_at = models.DateTimeField("发布时间", null=True, blank=True)
    directory_path = models.CharField("原始目录", max_length=1000, blank=True)
    raw_sha256 = models.CharField("原文件哈希", max_length=64, blank=True)
    normalized_sha256 = models.CharField("规范正文哈希", max_length=64, blank=True)
    action = models.CharField(
        "预览结论",
        max_length=20,
        choices=ACTION_CHOICES,
        default=ACTION_ERROR,
    )
    status = models.CharField(
        "处理状态",
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_PREVIEWED,
    )
    asset_count = models.PositiveIntegerField("图片数", default=0)
    asset_bytes = models.PositiveBigIntegerField("图片字节", default=0)
    warnings = models.JSONField("格式警告", default=list, blank=True)
    error_message = models.TextField("错误", blank=True)
    details = models.JSONField("解析详情", default=dict, blank=True)
    document = models.ForeignKey(
        KnowledgeDocument,
        verbose_name="导入后的文档",
        on_delete=models.SET_NULL,
        related_name="import_items",
        null=True,
        blank=True,
    )
    revision = models.ForeignKey(
        KnowledgeRevision,
        verbose_name="导入产生的版本",
        on_delete=models.SET_NULL,
        related_name="import_items",
        null=True,
        blank=True,
    )
    previous_revision = models.ForeignKey(
        KnowledgeRevision,
        verbose_name="导入前版本",
        on_delete=models.SET_NULL,
        related_name="superseded_by_import_items",
        null=True,
        blank=True,
    )
    previous_state = models.JSONField("导入前元数据", default=dict, blank=True)
    created_at = models.DateTimeField("检查时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        verbose_name = "知识导入项目"
        verbose_name_plural = "知识导入项目"
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(
                fields=["batch", "relative_path"],
                name="unique_knowledge_import_item_path",
            )
        ]
        indexes = [
            models.Index(fields=["batch", "action"]),
            models.Index(fields=["batch", "status"]),
            models.Index(fields=["external_id"]),
            models.Index(fields=["normalized_sha256"]),
        ]

    def __str__(self):
        return self.title or self.relative_path


class KnowledgeArtifact(TimestampedModel):
    TYPE_MANUAL = "manual"
    TYPE_MIND_MAP = "mind_map"
    TYPE_CHOICES = [
        (TYPE_MANUAL, "专题知识手册"),
        (TYPE_MIND_MAP, "交互式知识体系图"),
    ]

    STATUS_PENDING = "pending"
    STATUS_CONFIRMED = "confirmed"
    STATUS_SUPERSEDED = "superseded"
    STATUS_CHOICES = [
        (STATUS_PENDING, "AI 生成 · 待人工确认"),
        (STATUS_CONFIRMED, "已人工确认"),
        (STATUS_SUPERSEDED, "已被新成果替代"),
    ]

    family = models.ForeignKey(
        Family,
        verbose_name="所属家庭",
        on_delete=models.CASCADE,
        related_name="knowledge_artifacts",
    )
    owner = models.ForeignKey(
        FamilyMember,
        verbose_name="成果所有者",
        on_delete=models.PROTECT,
        related_name="knowledge_artifacts",
    )
    person_name = models.CharField("关联人物", max_length=300)
    normalized_person_name = models.CharField("规范人物名", max_length=300)
    artifact_type = models.CharField("成果类型", max_length=30, choices=TYPE_CHOICES)
    title = models.CharField("成果标题", max_length=500)
    description = models.TextField("成果说明", blank=True)
    visibility = models.CharField(
        "可见范围",
        max_length=20,
        choices=KnowledgeVisibility.choices,
        default=KnowledgeVisibility.FAMILY,
    )
    status = models.CharField(
        "确认状态",
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_PENDING,
    )
    current_version = models.ForeignKey(
        "KnowledgeArtifactVersion",
        verbose_name="当前成果版本",
        on_delete=models.SET_NULL,
        related_name="+",
        null=True,
        blank=True,
    )
    confirmed_by = models.ForeignKey(
        FamilyMember,
        verbose_name="确认人",
        on_delete=models.SET_NULL,
        related_name="confirmed_knowledge_artifacts",
        null=True,
        blank=True,
    )
    confirmed_at = models.DateTimeField("确认时间", null=True, blank=True)

    class Meta:
        verbose_name = "AI 专题成果"
        verbose_name_plural = "AI 专题成果"
        ordering = ["person_name", "artifact_type", "title", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["family", "normalized_person_name", "artifact_type"],
                name="unique_knowledge_artifact_type_per_person",
            )
        ]
        indexes = [
            models.Index(fields=["family", "visibility", "owner"]),
            models.Index(fields=["family", "normalized_person_name", "status"]),
        ]

    def save(self, *args, **kwargs):
        self.person_name = " ".join(str(self.person_name or "").strip().split())
        self.normalized_person_name = normalize_taxonomy_name(self.person_name)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.title


class KnowledgeArtifactVersion(models.Model):
    artifact = models.ForeignKey(
        KnowledgeArtifact,
        verbose_name="专题成果",
        on_delete=models.CASCADE,
        related_name="versions",
    )
    version_number = models.PositiveIntegerField("版本号")
    original_file = models.FileField(
        "AI 生成原文件",
        storage=protected_knowledge_storage,
        upload_to=artifact_original_upload_to,
        max_length=1000,
    )
    rendered_file = models.FileField(
        "安全展示版本",
        storage=protected_knowledge_storage,
        upload_to=artifact_rendered_upload_to,
        max_length=1000,
        blank=True,
    )
    original_name = models.CharField("原始文件名", max_length=300)
    content_hash = models.CharField("原文件 SHA-256", max_length=64)
    byte_size = models.PositiveBigIntegerField("原文件大小")
    plain_text = models.TextField("可搜索文本", blank=True)
    generator_name = models.CharField("生成工具", max_length=100, blank=True)
    model_name = models.CharField("模型", max_length=200, blank=True)
    prompt_version = models.CharField("提示词版本", max_length=100, blank=True)
    generated_at = models.DateTimeField("生成时间", null=True, blank=True)
    source_article_count = models.PositiveIntegerField("声明来源文章数", default=0)
    source_cutoff_date = models.DateField("来源截止日期", null=True, blank=True)
    source_snapshot = models.JSONField("来源版本快照", default=list, blank=True)
    reference_count = models.PositiveIntegerField("引用数", default=0)
    matched_reference_count = models.PositiveIntegerField("已匹配引用数", default=0)
    ambiguous_reference_count = models.PositiveIntegerField("重复待核对引用数", default=0)
    unmatched_reference_count = models.PositiveIntegerField("未匹配引用数", default=0)
    created_by = models.ForeignKey(
        FamilyMember,
        verbose_name="上传人",
        on_delete=models.PROTECT,
        related_name="created_knowledge_artifact_versions",
    )
    created_at = models.DateTimeField("保存时间", auto_now_add=True)

    class Meta:
        verbose_name = "AI 专题成果版本"
        verbose_name_plural = "AI 专题成果版本"
        ordering = ["-version_number", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["artifact", "version_number"],
                name="unique_knowledge_artifact_version_number",
            ),
            models.UniqueConstraint(
                fields=["artifact", "content_hash"],
                name="unique_knowledge_artifact_content_hash",
            ),
        ]

    def __str__(self):
        return f"{self.artifact} · v{self.version_number}"


class KnowledgeArtifactEvidence(models.Model):
    STATUS_MATCHED = "matched"
    STATUS_AMBIGUOUS = "ambiguous"
    STATUS_UNMATCHED = "unmatched"
    STATUS_CHOICES = [
        (STATUS_MATCHED, "已映射原文"),
        (STATUS_AMBIGUOUS, "存在重复候选"),
        (STATUS_UNMATCHED, "未找到原文"),
    ]

    version = models.ForeignKey(
        KnowledgeArtifactVersion,
        verbose_name="成果版本",
        on_delete=models.CASCADE,
        related_name="evidence_links",
    )
    reference_key = models.CharField("稳定引用键", max_length=64)
    citation_text = models.CharField("引用原文", max_length=1000)
    citation_title = models.CharField("引用文章标题", max_length=500)
    citation_date = models.DateField("引用文章日期", null=True, blank=True)
    status = models.CharField("映射状态", max_length=20, choices=STATUS_CHOICES)
    match_method = models.CharField("匹配方式", max_length=50, blank=True)
    document = models.ForeignKey(
        KnowledgeDocument,
        verbose_name="映射文档",
        on_delete=models.SET_NULL,
        related_name="artifact_evidence_links",
        null=True,
        blank=True,
    )
    revision = models.ForeignKey(
        KnowledgeRevision,
        verbose_name="映射内容版本",
        on_delete=models.SET_NULL,
        related_name="artifact_evidence_links",
        null=True,
        blank=True,
    )
    candidate_document_ids = models.JSONField("候选文档 ID", default=list, blank=True)
    created_at = models.DateTimeField("建立时间", auto_now_add=True)

    class Meta:
        verbose_name = "专题成果引用证据"
        verbose_name_plural = "专题成果引用证据"
        ordering = ["citation_date", "citation_title", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["version", "reference_key"],
                name="unique_knowledge_artifact_reference",
            )
        ]
        indexes = [
            models.Index(fields=["version", "status"]),
            models.Index(fields=["document", "revision"]),
        ]

    def __str__(self):
        return self.citation_text


class KnowledgeSearchEntry(TimestampedModel):
    KIND_DOCUMENT = "document"
    KIND_INVESTMENT_NOTE = "investment_note"
    KIND_ARTIFACT = "artifact"
    KIND_CHOICES = [
        (KIND_DOCUMENT, "知识文档"),
        (KIND_INVESTMENT_NOTE, "随手记"),
        (KIND_ARTIFACT, "AI 专题成果"),
    ]

    family = models.ForeignKey(
        Family,
        verbose_name="所属家庭",
        on_delete=models.CASCADE,
        related_name="knowledge_search_entries",
    )
    owner = models.ForeignKey(
        FamilyMember,
        verbose_name="资料所有者",
        on_delete=models.CASCADE,
        related_name="knowledge_search_entries",
    )
    item_kind = models.CharField("条目类型", max_length=30, choices=KIND_CHOICES)
    object_id = models.CharField("原业务对象 ID", max_length=100)
    document = models.OneToOneField(
        KnowledgeDocument,
        verbose_name="知识文档",
        on_delete=models.CASCADE,
        related_name="search_entry",
        null=True,
        blank=True,
    )
    artifact = models.OneToOneField(
        KnowledgeArtifact,
        verbose_name="AI 专题成果",
        on_delete=models.CASCADE,
        related_name="search_entry",
        null=True,
        blank=True,
    )
    visibility = models.CharField(
        "可见范围",
        max_length=20,
        choices=KnowledgeVisibility.choices,
    )
    title = models.CharField("标题", max_length=500)
    body = models.TextField("正文索引", blank=True)
    summary = models.TextField("摘要索引", blank=True)
    source_kind = models.CharField("来源类型", max_length=30)
    source_name = models.CharField("来源名称", max_length=300)
    author_name = models.CharField("作者", max_length=200, blank=True)
    category = models.CharField("分类", max_length=100, blank=True)
    tags = models.JSONField("标签", default=list, blank=True)
    tags_text = models.TextField("标签检索文本", blank=True)
    searchable_text = models.TextField("统一检索文本", blank=True)
    curation_status = models.CharField(
        "整理状态",
        max_length=30,
        choices=KnowledgeDocument.CURATION_CHOICES,
        blank=True,
    )
    knowledge_status = models.CharField(
        "知识状态",
        max_length=20,
        choices=KnowledgeDocument.KNOWLEDGE_STATUS_CHOICES,
        default=KnowledgeDocument.KNOWLEDGE_INCLUDED,
    )
    content_time = models.DateTimeField("内容时间", null=True, blank=True)

    class Meta:
        verbose_name = "知识搜索投影"
        verbose_name_plural = "知识搜索投影"
        ordering = ["-content_time", "-updated_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["family", "item_kind", "object_id"],
                name="unique_knowledge_search_projection",
            )
        ]
        indexes = [
            models.Index(fields=["family", "visibility", "owner"]),
            models.Index(fields=["family", "source_kind", "content_time"]),
            models.Index(fields=["family", "curation_status"]),
            models.Index(fields=["family", "knowledge_status"]),
        ]

    def __str__(self):
        return self.title
