from django.db import models
from django.utils import timezone

from family_core.models import Family, FamilyMember, TimestampedModel


class InvestmentNoteType(TimestampedModel):
    CODE_TRADE = "trade"
    CODE_STRATEGY = "strategy"
    CODE_RESEARCH = "research"
    CODE_PSYCHOLOGY = "psychology"
    CODE_OTHER = "other"

    name = models.CharField("类型名称", max_length=50, unique=True)
    code = models.SlugField(
        "类型编码",
        max_length=50,
        unique=True,
        help_text="用于筛选和样式识别，建议使用简短的小写英文，保存后尽量不要修改。",
    )
    sort_order = models.PositiveIntegerField("排序", default=100)
    is_active = models.BooleanField("是否启用", default=True)
    remark = models.CharField("备注", max_length=200, blank=True)

    class Meta:
        verbose_name = "投资笔记类型"
        verbose_name_plural = "投资笔记类型"
        ordering = ["sort_order", "id"]

    def __str__(self):
        return self.name


class InvestmentNote(TimestampedModel):
    TYPE_TRADE = InvestmentNoteType.CODE_TRADE
    TYPE_STRATEGY = InvestmentNoteType.CODE_STRATEGY
    TYPE_RESEARCH = InvestmentNoteType.CODE_RESEARCH
    TYPE_PSYCHOLOGY = InvestmentNoteType.CODE_PSYCHOLOGY
    TYPE_OTHER = InvestmentNoteType.CODE_OTHER

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
    note_type = models.ForeignKey(
        InvestmentNoteType,
        verbose_name="笔记类型",
        on_delete=models.PROTECT,
        related_name="investment_notes",
    )
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
