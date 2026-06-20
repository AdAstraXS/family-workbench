from django.db import models

from family_core.models import Family, FamilyMember, TimestampedModel


class InvestmentNote(TimestampedModel):
    family = models.ForeignKey(Family, verbose_name="所属家庭", on_delete=models.CASCADE, related_name="investment_notes")
    member = models.ForeignKey(FamilyMember, verbose_name="作者", on_delete=models.CASCADE, related_name="investment_notes")
    title = models.CharField("标题", max_length=200)
    content = models.TextField("内容", blank=True)
    note_type = models.CharField("笔记类型", max_length=50, default="general")
    visibility = models.CharField("可见范围", max_length=20, default="private")
    tags = models.JSONField("标签", default=list, blank=True)
    ai_summary = models.TextField("AI 总结", blank=True)
    remark = models.TextField("备注", blank=True)
    extra_data = models.JSONField("扩展字段", default=dict, blank=True)

    class Meta:
        verbose_name = "投资笔记"
        verbose_name_plural = "投资笔记"
        indexes = [
            models.Index(fields=["family", "member", "note_type", "created_at"]),
        ]

    def __str__(self):
        return self.title
