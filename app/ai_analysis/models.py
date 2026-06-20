from django.db import models

from family_core.models import Family, FamilyMember, TimestampedModel


class AiProvider(TimestampedModel):
    name = models.CharField("服务商名称", max_length=100)
    provider_type = models.CharField("服务商类型", max_length=50)
    base_url = models.URLField("API 地址", max_length=500, blank=True)
    model_name = models.CharField("默认模型", max_length=100, blank=True)
    is_active = models.BooleanField("是否启用", default=True)
    extra_data = models.JSONField("扩展字段", default=dict, blank=True)

    class Meta:
        verbose_name = "AI 服务商"
        verbose_name_plural = "AI 服务商"

    def __str__(self):
        return f"{self.name} {self.model_name}".strip()


class AiAnalysisRequest(TimestampedModel):
    STATUS_PENDING = "pending"
    STATUS_SUCCESS = "success"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_PENDING, "等待中"),
        (STATUS_SUCCESS, "成功"),
        (STATUS_FAILED, "失败"),
    ]

    family = models.ForeignKey(Family, verbose_name="所属家庭", on_delete=models.CASCADE, related_name="ai_requests")
    member = models.ForeignKey(FamilyMember, verbose_name="发起成员", on_delete=models.CASCADE, related_name="ai_requests")
    provider = models.ForeignKey(AiProvider, verbose_name="AI 服务商", on_delete=models.SET_NULL, related_name="requests", null=True, blank=True)
    module = models.CharField("分析模块", max_length=50)
    analysis_type = models.CharField("分析类型", max_length=100, blank=True)
    scope = models.JSONField("分析范围", default=dict, blank=True)
    prompt = models.TextField("提示词")
    sanitized_input = models.JSONField("脱敏后的输入数据", default=dict, blank=True)
    status = models.CharField("状态", max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    error_message = models.TextField("错误信息", blank=True)

    class Meta:
        verbose_name = "AI 分析请求"
        verbose_name_plural = "AI 分析请求"
        indexes = [
            models.Index(fields=["family", "member", "module", "created_at"]),
            models.Index(fields=["status"]),
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
