"""Family-scoped subscriptions and immutable source text for long-form intelligence."""
from decimal import Decimal

from django.db import models
from django.core.validators import MinValueValidator, MaxValueValidator
from family_core.models import Family, TimestampedModel


class ProgramSettings(TimestampedModel):
    family = models.OneToOneField(Family, on_delete=models.CASCADE)
    encrypted_credentials = models.TextField(blank=True, editable=False)
    workspace_id = models.CharField(max_length=80, blank=True)
    public_base_url = models.URLField(blank=True)
    allow_asr = models.BooleanField(default=False)
    allow_summary = models.BooleanField(default=False)
    summary_provider = models.ForeignKey('ai_analysis.AiProvider', null=True, blank=True, on_delete=models.SET_NULL)
    monthly_asr_cny = models.DecimalField(max_digits=8, decimal_places=2, default=20, validators=[MinValueValidator(Decimal('0.01'))])
    monthly_summary_usd = models.DecimalField(max_digits=8, decimal_places=2, default=2, validators=[MinValueValidator(Decimal('0.01'))])
    max_audio_minutes = models.PositiveIntegerField(default=180, validators=[MinValueValidator(1), MaxValueValidator(240)])
    configured_by = models.ForeignKey('family_core.FamilyMember', null=True, on_delete=models.SET_NULL)


class ProgramSubscription(TimestampedModel):
    family = models.ForeignKey(Family, on_delete=models.CASCADE)
    code = models.CharField(max_length=40)
    enabled = models.BooleanField(default=True)
    auto_process = models.BooleanField(default=True)
    # On first subscription only the latest three entries are collected. Older history is never billed automatically.
    last_checked_at = models.DateTimeField(null=True, blank=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=300, blank=True)
    lease_until = models.DateTimeField(null=True, blank=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=['family', 'code'], name='unique_family_program')]


class ProgramEntry(TimestampedModel):
    STATES = [('new', '待获取正文'), ('fetching', '正在获取'), ('asr_submit', '转写提交中'),
              ('asr_wait', '转写中'), ('text_ready', '文字稿可读'), ('summarizing', '正在整理'),
              ('ready', '整理完成'), ('waiting_config', '等待服务配置'), ('failed', '需要处理'), ('uncertain', '提交结果待核对')]
    subscription = models.ForeignKey(ProgramSubscription, related_name='entries', on_delete=models.PROTECT)
    external_id = models.CharField(max_length=200)
    title = models.CharField(max_length=500)
    url = models.URLField(max_length=2000)
    audio_url = models.URLField(max_length=3000, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    duration_seconds = models.PositiveIntegerField(default=0)
    state = models.CharField(max_length=20, choices=STATES, default='new')
    requested = models.BooleanField(default=True)
    last_error = models.CharField(max_length=300, blank=True)
    lease_until = models.DateTimeField(null=True, blank=True)
    task_id = models.CharField(max_length=150, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    asr_reserved_cny = models.DecimalField(max_digits=10, decimal_places=4, default=0)
    audio_file = models.FileField(upload_to='intelligence/encrypted-audio/%Y/%m/', blank=True)
    audio_expires_at = models.DateTimeField(null=True, blank=True)
    audio_mime = models.CharField(max_length=60, blank=True)
    current_revision = models.ForeignKey('ProgramRevision', null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    class Meta:
        ordering = ['-published_at', '-pk']
        constraints = [models.UniqueConstraint(fields=['subscription', 'external_id'], name='unique_program_entry')]


class ProgramRevision(TimestampedModel):
    entry = models.ForeignKey(ProgramEntry, related_name='revisions', on_delete=models.CASCADE)
    content_hash = models.CharField(max_length=64)
    origin = models.CharField(max_length=40)  # publisher / youtube_caption / fun-asr / manual
    source_url = models.URLField(max_length=2000)
    text = models.TextField()
    segments = models.JSONField(default=list)  # text, start_ms/end_ms (null for prose)
    provider_model = models.CharField(max_length=100, blank=True)
    summary = models.JSONField(default=dict, blank=True)
    summary_complete = models.BooleanField(default=False)
    archived_document = models.ForeignKey('knowledge.KnowledgeDocument', null=True, blank=True, on_delete=models.PROTECT)
    class Meta:
        constraints = [models.UniqueConstraint(fields=['entry', 'content_hash'], name='unique_program_revision')]

    @property
    def origin_label(self):
        return {'publisher': '出版方原文', 'youtube_caption': '公开视频字幕', 'fun-asr': '百炼音频转写', 'manual': '手动导入文字稿'}.get(self.origin, self.origin)


class ProgramSummaryChunk(TimestampedModel):
    revision = models.ForeignKey(ProgramRevision, related_name='chunks', on_delete=models.CASCADE)
    number = models.PositiveIntegerField()
    status = models.CharField(max_length=20, default='pending')
    result = models.JSONField(default=dict)
    provider = models.ForeignKey('ai_analysis.AiProvider', null=True, on_delete=models.SET_NULL)
    model_name = models.CharField(max_length=100, blank=True)
    prompt_version = models.CharField(max_length=40, default='program-summary-v1')
    reserved_usd = models.DecimalField(max_digits=10, decimal_places=6, default=0)
    tokens_used = models.PositiveIntegerField(default=0)
    class Meta:
        constraints = [models.UniqueConstraint(fields=['revision', 'number'], name='unique_program_chunk')]
