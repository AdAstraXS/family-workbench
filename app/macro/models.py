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


class MacroSourceMapping(models.Model):
    """Versioned dictionary entry; legacy MacroDataPoint rows remain untouched."""

    indicator = models.OneToOneField(MacroIndicator, on_delete=models.PROTECT, related_name="mapping")
    provider = models.CharField("来源", max_length=30)
    group = models.CharField("采集组", max_length=80)
    definition = models.JSONField("口径及字段映射")
    definition_hash = models.CharField(max_length=64)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return str(self.indicator)


class MacroImportRun(models.Model):
    group = models.CharField("采集组", max_length=80)
    status = models.CharField("状态", max_length=20, default="running", choices=[
        ("running", "运行中"), ("success", "成功"), ("failed", "失败"),
    ])
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True)
    summary = models.JSONField("统计", default=dict)
    error = models.CharField("错误", max_length=500, blank=True)


class MacroObservation(models.Model):
    mapping = models.ForeignKey(MacroSourceMapping, on_delete=models.PROTECT, related_name="observations")
    geography = models.CharField("地区", max_length=50, default="全国")
    period_date = models.DateField("统计期（期初）")
    value = models.DecimalField("数值", max_digits=24, decimal_places=8, null=True)
    release_date = models.DateField("官方发布日期", null=True)
    first_seen_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField()
    revision = models.PositiveIntegerField(default=1)
    fingerprint = models.CharField(max_length=64)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=["mapping", "geography", "period_date"], name="unique_macro_observation",
        )]
        ordering = ["-period_date", "geography"]


class MacroObservationRevision(models.Model):
    observation = models.ForeignKey(MacroObservation, on_delete=models.PROTECT, related_name="revisions")
    run = models.ForeignKey(MacroImportRun, on_delete=models.PROTECT)
    number = models.PositiveIntegerField()
    value = models.DecimalField(max_digits=24, decimal_places=8, null=True)
    release_date = models.DateField(null=True)
    observed_at = models.DateTimeField(auto_now_add=True)
    source_url = models.URLField(max_length=1000)
    source_hash = models.CharField("响应校验值", max_length=64)
    evidence = models.JSONField("来源字段及口径")

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=["observation", "number"], name="unique_macro_revision",
        )]
        ordering = ["-number"]


class MacroMaintenanceRun(models.Model):
    mode = models.CharField("任务", max_length=20)
    status = models.CharField("状态", max_length=20, default="running", choices=[
        ("running", "运行中"), ("success", "成功"), ("failed", "失败"), ("interrupted", "已中断"),
    ])
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True)
    summary = models.JSONField(default=dict)
    error = models.CharField(max_length=500, blank=True)


class MacroOfficialReport(models.Model):
    group = models.CharField("来源", max_length=80)
    url = models.URLField("官方报告", max_length=1000, unique=True)
    title = models.CharField(max_length=300)
    period_date = models.DateField(null=True)
    release_date = models.DateField(null=True)
    content_hash = models.CharField(max_length=64, blank=True)
    status = models.CharField(max_length=20, default="pending")
    error = models.CharField(max_length=500, blank=True)
    summary = models.JSONField(default=dict)
    checked_at = models.DateTimeField(null=True)
    imported_at = models.DateTimeField(null=True)


class MacroCalendarSnapshot(models.Model):
    agency = models.CharField(max_length=30)
    source_url = models.URLField(max_length=1000)
    content_hash = models.CharField(max_length=64)
    payload = models.JSONField("已核验计划日历")
    checked_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["agency", "content_hash"], name="unique_macro_calendar_snapshot")]
