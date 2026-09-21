from django.db import models

from family_core.models import Family, FamilyMember, TimestampedModel
from portfolio.models import Security


# 官方资料来源（M2A-1 固定契约，供后续 connector 复用）。
SOURCE_SEC = "sec"
SOURCE_MICROSOFT_IR = "microsoft_ir"
SOURCE_CHOICES = [
    (SOURCE_SEC, "SEC EDGAR"),
    (SOURCE_MICROSOFT_IR, "Microsoft IR"),
]

# 官方资料类型。
DOC_TYPE_10K = "10-k"
DOC_TYPE_10Q = "10-q"
DOC_TYPE_8K = "8-k"
DOC_TYPE_ANNUAL_REPORT = "annual_report"
DOC_TYPE_EARNINGS_RELEASE = "earnings_release"
DOC_TYPE_EARNINGS_CALL = "earnings_call"
DOC_TYPE_INVESTOR_UPDATE = "investor_update"
DOC_TYPE_OTHER = "other"
DOCUMENT_TYPE_CHOICES = [
    (DOC_TYPE_10K, "10-K 年报"),
    (DOC_TYPE_10Q, "10-Q 季报"),
    (DOC_TYPE_8K, "8-K 重大事件"),
    (DOC_TYPE_ANNUAL_REPORT, "年度报告"),
    (DOC_TYPE_EARNINGS_RELEASE, "财报新闻稿"),
    (DOC_TYPE_EARNINGS_CALL, "财报电话会/网络直播"),
    (DOC_TYPE_INVESTOR_UPDATE, "投资者公告"),
    (DOC_TYPE_OTHER, "其他"),
]


class ResearchDossier(TimestampedModel):
    """私密公司研究档案：一个成员对一个证券一份。

    family/owner/security 由后端从当前登录成员与所选证券赋值，
    创建后不可由编辑接口变更。探索态没有判断版本，首次正式确认时
    才填写 initial_thesis 并创建第一版判断。
    """

    family = models.ForeignKey(
        Family,
        verbose_name="所属家庭",
        on_delete=models.PROTECT,
        related_name="research_dossiers",
    )
    owner = models.ForeignKey(
        FamilyMember,
        verbose_name="档案所有人",
        on_delete=models.PROTECT,
        related_name="research_dossiers",
    )
    security = models.ForeignKey(
        Security,
        verbose_name="研究标的",
        on_delete=models.PROTECT,
        related_name="research_dossiers",
    )
    initial_thesis = models.TextField("原始持有理由", blank=True)
    current_revision = models.ForeignKey(
        "ResearchThesisRevision",
        verbose_name="当前判断版本",
        on_delete=models.PROTECT,
        related_name="+",
        null=True,
        blank=True,
    )

    class Meta:
        verbose_name = "公司研究档案"
        verbose_name_plural = "公司研究档案"
        constraints = [
            models.UniqueConstraint(
                fields=["owner", "security"],
                name="unique_research_dossier_per_owner_security",
            )
        ]

    def __str__(self):
        return f"{self.owner} - {self.security}"


class ResearchThesisRevision(models.Model):
    """判断版本：只追加，不提供修改/删除旧版本的操作。"""

    dossier = models.ForeignKey(
        ResearchDossier,
        verbose_name="所属档案",
        on_delete=models.PROTECT,
        related_name="revisions",
    )
    revision_number = models.PositiveIntegerField("版本号")
    thesis = models.TextField("当前判断")
    pillars = models.JSONField("关键假设", default=list, blank=True)
    questions = models.JSONField("待验证问题", default=list, blank=True)
    change_reason = models.CharField("修改原因", max_length=500, blank=True)
    created_by = models.ForeignKey(
        FamilyMember,
        verbose_name="保存人",
        on_delete=models.PROTECT,
        related_name="research_thesis_revisions",
    )
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        verbose_name = "判断版本"
        verbose_name_plural = "判断版本"
        ordering = ["-revision_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["dossier", "revision_number"],
                name="unique_thesis_revision_number_per_dossier",
            )
        ]

    def __str__(self):
        return f"{self.dossier} 第 {self.revision_number} 版"


class OfficialResearchDocument(TimestampedModel):
    """官方研究资料：按 Security 共用，避免同一证券的多个私人档案重复下载。

    本批只定义存储契约：模型不发网络请求、不自动生成 URL、不在 save() 中做同步；
    source_url 的 HTTPS 官方域名校验由服务层完成。
    """

    security = models.ForeignKey(
        Security,
        verbose_name="所属证券",
        on_delete=models.PROTECT,
        related_name="official_research_documents",
    )
    source = models.CharField(
        "来源", max_length=32, choices=SOURCE_CHOICES
    )
    external_id = models.CharField("来源内稳定标识", max_length=255)
    document_type = models.CharField(
        "资料类型", max_length=32, choices=DOCUMENT_TYPE_CHOICES
    )
    title = models.CharField("标题", max_length=500)
    source_url = models.URLField("来源链接", max_length=1000)
    published_at = models.DateField("发布日期", null=True, blank=True)
    period_end = models.DateField("报告期截止日", null=True, blank=True)
    content_text = models.TextField("正文纯文本", null=True, blank=True)
    content_sha256 = models.CharField("正文 SHA-256", max_length=64, null=True, blank=True)
    fetched_at = models.DateTimeField("抓取时间", null=True, blank=True)
    metadata = models.JSONField("来源元数据", default=dict, blank=True)

    class Meta:
        verbose_name = "官方研究资料"
        verbose_name_plural = "官方研究资料"
        ordering = ["-published_at", "-pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["source", "external_id"],
                name="unique_official_document_source_external_id",
            )
        ]
        indexes = [
            models.Index(fields=["security", "-published_at"]),
            models.Index(fields=["source", "document_type"]),
        ]

    def __str__(self):
        return f"{self.security} {self.get_source_display()} {self.external_id}"


class OfficialResearchContentVersion(models.Model):
    """按需取得的 SEC 原件和规范正文；旧版本永不由业务入口覆盖。"""

    document = models.ForeignKey(
        OfficialResearchDocument, on_delete=models.PROTECT,
        related_name="content_versions", verbose_name="官方资料",
    )
    version_number = models.PositiveIntegerField("版本号")
    source_url = models.URLField("获取时来源链接", max_length=1000)
    raw_sha256 = models.CharField("原始响应 SHA-256", max_length=64)
    raw_gzip = models.BinaryField("原始 HTML（gzip）")
    content_text = models.TextField("规范正文")
    content_sha256 = models.CharField("规范正文 SHA-256", max_length=64)
    extractor_version = models.CharField("提取器版本", max_length=32)
    fetched_at = models.DateTimeField("获取时间")

    class Meta:
        verbose_name = "官方资料正文版本"
        verbose_name_plural = "官方资料正文版本"
        ordering = ["-version_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["document", "version_number"],
                name="unique_research_content_version_number",
            ),
        ]


class ResearchSourceState(TimestampedModel):
    """来源同步状态：每个证券每个来源一条，记录游标与最近同步结果。

    last_error 最多由服务截断为 2000 字符，禁止保存响应正文和凭据。
    """

    security = models.ForeignKey(
        Security,
        verbose_name="所属证券",
        on_delete=models.PROTECT,
        related_name="research_source_states",
    )
    source = models.CharField(
        "来源", max_length=32, choices=SOURCE_CHOICES
    )
    external_company_id = models.CharField(
        "来源内公司标识", max_length=64, null=True, blank=True
    )
    cursor = models.JSONField("同步游标", default=dict, blank=True)
    last_checked_at = models.DateTimeField("最近检查时间", null=True, blank=True)
    last_success_at = models.DateTimeField("最近成功时间", null=True, blank=True)
    last_error = models.TextField("最近错误", null=True, blank=True)

    class Meta:
        verbose_name = "来源同步状态"
        verbose_name_plural = "来源同步状态"
        constraints = [
            models.UniqueConstraint(
                fields=["security", "source"],
                name="unique_research_source_state_security_source",
            )
        ]

    def __str__(self):
        return f"{self.security} {self.get_source_display()} 同步状态"
