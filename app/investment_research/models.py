from django.db import models

from family_core.models import Family, FamilyMember, TimestampedModel
from portfolio.models import Security


class ResearchDossier(TimestampedModel):
    """私密公司研究档案：一个成员对一个证券一份。

    family/owner/security 由后端从当前登录成员与所选证券赋值，
    创建后不可由编辑接口变更。
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
    initial_thesis = models.TextField("原始持有理由")
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
