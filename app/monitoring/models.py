import uuid
from django.db import models
from django.core.validators import MinValueValidator
from django.utils import timezone


VENDORS = [('deepseek', 'DeepSeek'), ('zhipu', '智谱'), ('volcano', '火山方舟'), ('ali', '阿里百炼'), ('other', '其他')]
MODULES = {'global_ai': 'AI 检索与问答', 'knowledge': '知识整理', 'reading': '在线阅读',
           'investment_research': '投资研究', 'intelligence': 'AI 情报', 'programs': '精选订阅',
           'option_wheel': '期权分析', 'ipo': '图片识别'}


class UsageRecord(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    family = models.ForeignKey('family_core.Family', on_delete=models.PROTECT)
    provider = models.ForeignKey('ai_analysis.AiProvider', null=True, on_delete=models.SET_NULL)
    vendor = models.CharField(max_length=20, choices=VENDORS)
    model_name = models.CharField(max_length=150)
    module = models.CharField(max_length=40)
    source_ref = models.CharField(max_length=160, blank=True)
    vendor_request_id = models.CharField(max_length=160, blank=True)
    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True)
    status = models.CharField(max_length=16, default='pending', choices=[('pending','处理中'),('confirmed','已确认'),('unknown','用量未确认')])
    input_tokens = models.PositiveBigIntegerField(null=True)
    output_tokens = models.PositiveBigIntegerField(null=True)
    cached_tokens = models.PositiveBigIntegerField(null=True)
    total_tokens = models.PositiveBigIntegerField(null=True)
    audio_seconds = models.DecimalField(max_digits=14, decimal_places=3, null=True)
    cost_cny = models.DecimalField(max_digits=18, decimal_places=8, null=True)
    price_snapshot = models.JSONField(default=dict)
    elapsed_ms = models.PositiveIntegerField(null=True)
    outcome = models.CharField(max_length=24, blank=True)

    class Meta:
        ordering = ['-started_at']
        indexes = [models.Index(fields=['family','started_at']), models.Index(fields=['vendor_request_id'])]
        verbose_name = '模型调用用量'
        verbose_name_plural = verbose_name

    @property
    def module_label(self):
        return MODULES.get(self.module, self.module)


class BalanceAccount(models.Model):
    family = models.ForeignKey('family_core.Family', on_delete=models.PROTECT)
    vendor = models.CharField(max_length=20, choices=VENDORS)
    account_key = models.CharField(max_length=100, default='default')
    label = models.CharField(max_length=100)
    key_env = models.CharField(max_length=100, blank=True)
    encrypted_credentials = models.TextField(blank=True, editable=False)
    balance_cny = models.DecimalField(max_digits=18, decimal_places=6, null=True)
    checked_at = models.DateTimeField(null=True)
    attempted_at = models.DateTimeField(null=True)
    status = models.CharField(max_length=20, default='unconfigured')
    message = models.CharField(max_length=200, blank=True)
    low_threshold = models.DecimalField(max_digits=12, decimal_places=2, default=20, validators=[MinValueValidator(0)])
    refresh_requested = models.BooleanField(default=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['family','vendor','account_key'], name='monitor_unique_account')]
        verbose_name = '服务商余额'
        verbose_name_plural = verbose_name


class HostSample(models.Model):
    sampled_at = models.DateTimeField(unique=True)
    received_at = models.DateTimeField(default=timezone.now)
    proxy_ok = models.BooleanField(default=False)
    session_id = models.CharField(max_length=100, blank=True)
    upload_total = models.PositiveBigIntegerField(null=True)
    download_total = models.PositiveBigIntegerField(null=True)
    upload_delta = models.PositiveBigIntegerField(null=True)
    download_delta = models.PositiveBigIntegerField(null=True)
    gap = models.BooleanField(default=False)
    subscriptions = models.JSONField(default=list)
    disk_total = models.PositiveBigIntegerField(null=True)
    disk_free = models.PositiveBigIntegerField(null=True)
    backup_at = models.DateTimeField(null=True)
    backup_name = models.CharField(max_length=200, blank=True)
    log_activity = models.JSONField(default=dict)
    message = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ['-sampled_at']
        verbose_name = 'NAS 采集快照'
        verbose_name_plural = verbose_name


class DownloadRecord(models.Model):
    family = models.ForeignKey('family_core.Family', on_delete=models.PROTECT)
    created_at = models.DateTimeField(default=timezone.now)
    entry_id = models.PositiveBigIntegerField()
    source = models.CharField(max_length=60)
    file_bytes = models.PositiveBigIntegerField()
    media_type = models.CharField(max_length=40)

    class Meta:
        ordering = ['-created_at']


class CollectorState(models.Model):
    key = models.CharField(max_length=30, primary_key=True, default='main')
    started_at = models.DateTimeField(null=True)
    finished_at = models.DateTimeField(null=True)
    status = models.CharField(max_length=20, default='waiting')
    message = models.CharField(max_length=200, blank=True)
