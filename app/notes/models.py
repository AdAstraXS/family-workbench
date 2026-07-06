from django.db import models
from django.utils import timezone

from family_core.models import Family, FamilyMember, TimestampedModel


class InvestmentNote(TimestampedModel):
    TYPE_TRADE = "trade"
    TYPE_STRATEGY = "strategy"
    TYPE_RESEARCH = "research"
    TYPE_PSYCHOLOGY = "psychology"
    TYPE_OTHER = "other"
    TYPE_CHOICES = [
        (TYPE_TRADE, "交易记录"),
        (TYPE_STRATEGY, "投资策略"),
        (TYPE_RESEARCH, "研究分析"),
        (TYPE_PSYCHOLOGY, "投资心理"),
        (TYPE_OTHER, "其他"),
    ]

    VISIBILITY_PRIVATE = "private"
    VISIBILITY_FAMILY = "family"
    VISIBILITY_CHOICES = [
        (VISIBILITY_PRIVATE, "仅自己"),
        (VISIBILITY_FAMILY, "家庭共享"),
    ]

    family = models.ForeignKey(Family, verbose_name="所属家庭", on_delete=models.CASCADE, related_name="investment_notes")
    member = models.ForeignKey(FamilyMember, verbose_name="作者", on_delete=models.CASCADE, related_name="investment_notes")
    title = models.CharField("标题", max_length=200)
    content = models.TextField("内容")
    note_type = models.CharField("笔记类型", max_length=50, choices=TYPE_CHOICES, default=TYPE_OTHER)
    note_date = models.DateField("笔记日期", default=timezone.localdate)
    visibility = models.CharField(
        "可见范围",
        max_length=20,
        choices=VISIBILITY_CHOICES,
        default=VISIBILITY_PRIVATE,
    )
    tags = models.JSONField("标签", default=list, blank=True)
    ai_summary = models.TextField("AI 总结", blank=True)
    remark = models.TextField("备注", blank=True)
    extra_data = models.JSONField("扩展字段", default=dict, blank=True)

    class Meta:
        verbose_name = "投资笔记"
        verbose_name_plural = "投资笔记"
        ordering = ["-note_date", "-updated_at"]
        indexes = [
            models.Index(fields=["family", "member", "note_type", "created_at"]),
        ]

    def __str__(self):
        return self.title
