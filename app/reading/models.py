import uuid

from django.db import models
from family_core.models import TimestampedModel


class Book(TimestampedModel):
    PRIVATE = "private"
    FAMILY = "family"
    VISIBILITY = [(PRIVATE, "仅自己"), (FAMILY, "家庭共享")]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    family = models.ForeignKey("family_core.Family", on_delete=models.PROTECT)
    owner = models.ForeignKey("family_core.FamilyMember", on_delete=models.PROTECT)
    title = models.CharField("书名", max_length=250)
    author = models.CharField("作者", max_length=250, blank=True)
    visibility = models.CharField("可见范围", max_length=10, choices=VISIBILITY, default=PRIVATE)
    description = models.TextField("简介", blank=True, max_length=5000)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "图书"
        verbose_name_plural = "图书"
        indexes = [models.Index(fields=["family", "visibility"])]

    def __str__(self):
        return self.title


class BookFile(TimestampedModel):
    STATES = [("queued", "等待处理"), ("processing", "正在解析"), ("ready", "可阅读"),
              ("failed", "处理失败"), ("unsupported", "已保存，阅读暂未接入")]
    book = models.OneToOneField(Book, on_delete=models.CASCADE, related_name="file")
    original_name = models.CharField(max_length=255)
    original_path = models.CharField(max_length=500)
    sha256 = models.CharField(max_length=64)
    size = models.PositiveBigIntegerField()
    format = models.CharField(max_length=10)
    status = models.CharField(max_length=15, choices=STATES, default="queued", db_index=True)
    error = models.CharField(max_length=500, blank=True)
    normalized_path = models.CharField(max_length=500, blank=True)
    normalizer_version = models.CharField(max_length=40, blank=True)
    # Exact immutable resources and ordered spine of the normalized EPUB.
    resources = models.JSONField(default=dict, blank=True)
    sections = models.JSONField(default=list, blank=True)
    text_status = models.CharField(max_length=20, default="unknown")
    page_count = models.PositiveIntegerField(default=0)
    lease = models.UUIDField(null=True, editable=False)
    processing_started_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "图书文件"
        verbose_name_plural = "图书文件"

    @property
    def reading_label(self):
        if self.format == "pdf" and self.status == "ready" and not self.page_count:
            return "可打开 · PDF 内容由阅读器校验"
        return self.get_status_display()


class ReadingImportRun(models.Model):
    file = models.ForeignKey(BookFile, on_delete=models.CASCADE, related_name="runs")
    token = models.UUIDField(default=uuid.uuid4, unique=True)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, default="processing")
    message = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ["-started_at"]


class ReadingPosition(TimestampedModel):
    book = models.ForeignKey(Book, on_delete=models.CASCADE, related_name="positions")
    member = models.ForeignKey("family_core.FamilyMember", on_delete=models.CASCADE)
    file_hash = models.CharField(max_length=64)
    location = models.JSONField(default=dict)
    progress = models.PositiveSmallIntegerField(default=0)  # 0..10000, display only
    revision = models.PositiveIntegerField(default=1)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["book", "member"], name="reading_position_member_book"),
                       models.CheckConstraint(condition=models.Q(progress__lte=10000), name="reading_progress_range")]
        verbose_name = "个人阅读位置"
        verbose_name_plural = "个人阅读位置"

    @property
    def percent(self):
        return round(self.progress / 100)


class Annotation(TimestampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    book = models.ForeignKey(Book, on_delete=models.PROTECT, related_name="annotations")
    author = models.ForeignKey("family_core.FamilyMember", on_delete=models.PROTECT)
    file_hash = models.CharField(max_length=64)
    normalizer_version = models.CharField(max_length=40)
    anchor = models.JSONField()
    quote = models.TextField(max_length=4000)
    note = models.TextField(max_length=10000, blank=True)
    visibility = models.CharField(max_length=10, choices=Book.VISIBILITY, default=Book.PRIVATE)
    revision = models.PositiveIntegerField(default=1)
    text_matched = models.BooleanField(default=False)

    class Meta:
        ordering = ["created_at", "pk"]
        verbose_name = "阅读批注"
        verbose_name_plural = "阅读批注"


class AnnotationComment(TimestampedModel):
    annotation = models.ForeignKey(Annotation, on_delete=models.PROTECT, related_name="comments")
    author = models.ForeignKey("family_core.FamilyMember", on_delete=models.PROTECT)
    body = models.TextField(max_length=5000)
    revision = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ["created_at", "pk"]


class ReadingPlan(TimestampedModel):
    member = models.ForeignKey("family_core.FamilyMember", on_delete=models.PROTECT)
    title = models.CharField("计划名称", max_length=200)
    target_date = models.DateField("目标日期")

    class Meta:
        ordering = ["target_date", "pk"]


class ReadingPlanItem(TimestampedModel):
    plan = models.ForeignKey(ReadingPlan, on_delete=models.CASCADE, related_name="items")
    book = models.ForeignKey(Book, on_delete=models.PROTECT, null=True, blank=True)
    title = models.CharField("书名", max_length=250)
    kind = models.CharField("阅读方式", max_length=20, choices=[("online","在线图书"),("paper","纸质书"),("external","外部阅读")])
    manual_progress = models.PositiveSmallIntegerField("手工进度（%）", default=0)
    completed_at = models.DateTimeField(null=True, blank=True)
    revision = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ["pk"]
        constraints = [models.CheckConstraint(condition=models.Q(manual_progress__lte=100), name="reading_manual_progress_range"),
                       models.UniqueConstraint(fields=["plan", "book"], name="reading_plan_unique_book")]


class ReadingArtifact(TimestampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    book = models.ForeignKey(Book, on_delete=models.PROTECT, related_name="artifacts")
    owner = models.ForeignKey("family_core.FamilyMember", on_delete=models.PROTECT)
    title = models.CharField(max_length=250)
    visibility = models.CharField(max_length=10, choices=Book.VISIBILITY, default=Book.PRIVATE)
    current_version = models.ForeignKey("ReadingArtifactVersion", on_delete=models.SET_NULL, related_name="+", null=True, blank=True)

    class Meta:
        ordering = ["-updated_at"]


class ReadingArtifactVersion(models.Model):
    artifact = models.ForeignKey(ReadingArtifact, on_delete=models.PROTECT, related_name="versions")
    number = models.PositiveIntegerField()
    title = models.CharField(max_length=250)
    data = models.JSONField(default=dict)
    original_path = models.CharField(max_length=500, blank=True)
    original_name = models.CharField(max_length=250, blank=True)
    sha256 = models.CharField(max_length=64)
    kind = models.CharField(max_length=20, choices=[("external","外部整理"),("manual","成员整理"),("ai","AI 提炼")])
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-number"]
        constraints = [models.UniqueConstraint(fields=["artifact","number"], name="reading_artifact_version"),
                       models.UniqueConstraint(fields=["artifact","sha256"], name="reading_artifact_content")]


class ReadingArchive(models.Model):
    version = models.OneToOneField(ReadingArtifactVersion,on_delete=models.PROTECT,related_name="archive")
    document = models.OneToOneField("knowledge.KnowledgeDocument",on_delete=models.PROTECT,related_name="reading_archive")
    created_at = models.DateTimeField(auto_now_add=True)


class ReadingAiJob(TimestampedModel):
    STATES = [("draft","等待确认范围"),("queued","等待生成"),("running","正在生成"),("success","生成完成"),
              ("failed","生成失败"),("cancelled","已取消")]
    id = models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    book = models.ForeignKey(Book,on_delete=models.PROTECT,related_name="ai_jobs")
    member = models.ForeignKey("family_core.FamilyMember",on_delete=models.PROTECT)
    provider = models.ForeignKey("ai_analysis.AiProvider",on_delete=models.PROTECT)
    status = models.CharField(max_length=15,choices=STATES,default="draft",db_index=True)
    provider_snapshot = models.JSONField()
    input_snapshot = models.JSONField()
    input_hash = models.CharField(max_length=64)
    estimated_tokens = models.PositiveIntegerField()
    estimated_usd = models.DecimalField(max_digits=12,decimal_places=6,null=True,blank=True)
    confirmed_at = models.DateTimeField(null=True,blank=True)
    started_at = models.DateTimeField(null=True,blank=True)
    finished_at = models.DateTimeField(null=True,blank=True)
    error = models.CharField(max_length=500,blank=True)
    result = models.JSONField(default=dict,blank=True)
    analysis_request = models.OneToOneField("ai_analysis.AiAnalysisRequest",on_delete=models.PROTECT,null=True,blank=True)
    artifact = models.ForeignKey(ReadingArtifact,on_delete=models.PROTECT,null=True,blank=True)

    class Meta:
        ordering = ["-created_at"]
