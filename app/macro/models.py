from django.db import models


class MacroIndicator(models.Model):
    country = models.CharField("国家/地区", max_length=20)
    code = models.CharField("指标代码", max_length=100)
    name = models.CharField("指标名称", max_length=200)
    category = models.CharField("分类", max_length=100, blank=True)
    frequency = models.CharField("频率", max_length=30, blank=True)
    unit = models.CharField("单位", max_length=50, blank=True)
    source = models.CharField("数据来源", max_length=100, blank=True)
    is_active = models.BooleanField("是否启用", default=True)
    extra_data = models.JSONField("扩展字段", default=dict, blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        verbose_name = "宏观指标"
        verbose_name_plural = "宏观指标"
        constraints = [
            models.UniqueConstraint(fields=["country", "code"], name="unique_macro_indicator")
        ]

    def __str__(self):
        return f"{self.country} {self.name}"


class MacroDataPoint(models.Model):
    indicator = models.ForeignKey(MacroIndicator, verbose_name="指标", on_delete=models.CASCADE, related_name="data_points")
    period_date = models.DateField("数据日期")
    value = models.DecimalField("数值", max_digits=24, decimal_places=8)
    revised_value = models.DecimalField("修正值", max_digits=24, decimal_places=8, null=True, blank=True)
    release_date = models.DateField("发布日期", null=True, blank=True)
    raw_data = models.JSONField("原始数据", default=dict, blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        verbose_name = "宏观数据点"
        verbose_name_plural = "宏观数据点"
        indexes = [
            models.Index(fields=["indicator", "period_date"]),
        ]

    def __str__(self):
        return f"{self.indicator} {self.period_date}"
