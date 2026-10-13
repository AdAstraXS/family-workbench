"""Manual household plans and separately sourced public fund disclosures."""
from decimal import Decimal, ROUND_HALF_UP
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator, MinValueValidator, MaxValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone

from family_core.models import Family, FamilyMember, TimestampedModel

CENT = Decimal("0.01")
FUND_CODE = RegexValidator(r"^[0-9]{6}$", "请输入完整的六位基金代码，保留开头的0。")


class FundReference(TimestampedModel):
    code = models.CharField("基金代码", max_length=6, unique=True, validators=[FUND_CODE])
    name = models.CharField("基金名称", max_length=200, blank=True)
    profile = models.JSONField("基金资料", default=dict, blank=True)
    profile_source = models.URLField("资料来源", max_length=500, blank=True)
    profile_fetched_at = models.DateTimeField("资料抓取时间", null=True, blank=True)
    latest_nav = models.DecimalField("最新单位净值", max_digits=20, decimal_places=6, null=True, blank=True)
    nav_date = models.DateField("净值日期", null=True, blank=True)
    nav_source = models.URLField("净值来源", max_length=500, blank=True)
    nav_fetched_at = models.DateTimeField("净值抓取时间", null=True, blank=True)
    last_attempt_at = models.DateTimeField("最近尝试时间", null=True, blank=True)
    refresh_errors = models.JSONField("更新异常", default=dict, blank=True)
    target_fund = models.ForeignKey("self", verbose_name="目标ETF", on_delete=models.PROTECT,
                                    null=True, blank=True, related_name="feeder_funds")
    target_source = models.URLField("目标ETF关系来源", max_length=500, blank=True)

    class Meta:
        ordering = ["code"]
        verbose_name = "定投基金资料"
        verbose_name_plural = "定投基金资料"
        constraints = [models.CheckConstraint(condition=Q(latest_nav__isnull=True) | Q(latest_nav__gt=0), name="fund_latest_nav_positive")]

    def __str__(self):
        return f"{self.code} {self.name or '资料待更新'}"


class FundNavRecord(models.Model):
    fund = models.ForeignKey(FundReference, on_delete=models.CASCADE, related_name="nav_records")
    nav_date = models.DateField("净值日期")
    unit_nav = models.DecimalField("单位净值", max_digits=20, decimal_places=6)
    source_url = models.URLField("来源", max_length=500)
    fetched_at = models.DateTimeField("抓取时间", default=timezone.now)

    class Meta:
        ordering = ["nav_date"]
        verbose_name = "基金净值历史"
        verbose_name_plural = "基金净值历史"
        constraints = [
            models.UniqueConstraint(fields=["fund", "nav_date"], name="fund_nav_unique_date"),
            models.CheckConstraint(condition=Q(unit_nav__gt=0), name="fund_nav_positive"),
        ]


class FundDisclosureSnapshot(TimestampedModel):
    fund = models.ForeignKey(FundReference, on_delete=models.CASCADE, related_name="disclosures")
    report_date = models.DateField("报告日期")
    allocations = models.JSONField("资产分布", default=list, blank=True)
    holdings = models.JSONField("披露的重仓股票", default=list, blank=True)
    allocation_source = models.URLField("资产分布来源", max_length=500, blank=True)
    holdings_source = models.URLField("持仓来源", max_length=500, blank=True)
    allocation_fetched_at = models.DateTimeField(null=True, blank=True)
    holdings_fetched_at = models.DateTimeField(null=True, blank=True)
    percentage_basis = models.CharField("比例口径", max_length=100, default="占基金资产净值比例")

    class Meta:
        ordering = ["-report_date"]
        verbose_name = "基金季度披露"
        verbose_name_plural = "基金季度披露"
        constraints = [models.UniqueConstraint(fields=["fund", "report_date"], name="fund_disclosure_unique_date")]


class FundDcaPlan(TimestampedModel):
    FREQUENCIES = [("day", "按日"), ("week", "按周"), ("month", "按月"), ("quarter", "按季度"), ("year", "按年")]
    ANNUAL_COUNTS = {"day": Decimal("252"), "week": Decimal("52"), "month": Decimal("12"), "quarter": Decimal("4"), "year": Decimal("1")}
    family = models.ForeignKey(Family, on_delete=models.PROTECT, related_name="fund_dca_plans")
    member = models.ForeignKey(FamilyMember, verbose_name="家庭成员", on_delete=models.PROTECT, related_name="fund_dca_plans")
    fund = models.ForeignKey(FundReference, on_delete=models.PROTECT, related_name="dca_plans")
    name = models.CharField("定投名称", max_length=100)
    start_date = models.DateField("定投起始日期")
    end_date = models.DateField("定投终止日期", null=True, blank=True)
    frequency = models.CharField("定投频率", max_length=10, choices=FREQUENCIES, default="month")
    amount = models.DecimalField("每次定投金额（元）", max_digits=20, decimal_places=2, validators=[MinValueValidator(Decimal("0"))])
    fee_rate = models.DecimalField("支付宝手续费比例（%）", max_digits=9, decimal_places=6,
                                  validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("100"))])
    balance = models.DecimalField("当前余额（元）", max_digits=20, decimal_places=2, null=True, blank=True,
                                 validators=[MinValueValidator(Decimal("0"))])
    balance_date = models.DateField("余额日期", null=True, blank=True)
    remark = models.TextField("备注", blank=True)
    revision = models.PositiveIntegerField("版本", default=1)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="created_fund_dca_plans")
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="updated_fund_dca_plans")

    class Meta:
        ordering = ["member__display_order", "member_id", "id"]
        verbose_name = "基金定投计划"
        verbose_name_plural = "基金定投计划"
        indexes = [models.Index(fields=["family", "member"], name="fund_dca_family_member")]
        constraints = [
            models.CheckConstraint(condition=Q(amount__gte=0), name="fund_dca_amount_nonnegative"),
            models.CheckConstraint(condition=Q(fee_rate__gte=0, fee_rate__lte=100), name="fund_dca_fee_rate_range"),
            models.CheckConstraint(condition=Q(balance__isnull=True) | Q(balance__gte=0), name="fund_dca_balance_nonnegative"),
            models.CheckConstraint(condition=Q(end_date__isnull=True) | Q(end_date__gte=models.F("start_date")), name="fund_dca_date_order"),
            models.CheckConstraint(condition=Q(balance__isnull=True, balance_date__isnull=True) | Q(balance__isnull=False, balance_date__isnull=False), name="fund_dca_balance_date_pair"),
        ]

    def clean(self):
        super().clean()
        if self.member_id and self.family_id and self.member.family_id != self.family_id:
            raise ValidationError({"member": "所选成员不属于当前家庭。"})
        if self.end_date and self.start_date and self.end_date < self.start_date:
            raise ValidationError({"end_date": "终止日期不能早于起始日期。"})
        if (self.balance is None) != (self.balance_date is None):
            raise ValidationError("余额与余额日期须同时填写或同时留空。")
        if self.balance_date and self.balance_date > timezone.localdate():
            raise ValidationError({"balance_date": "余额日期不能晚于今天。"})

    @property
    def annual_amount(self):
        return self.amount * self.ANNUAL_COUNTS[self.frequency]

    @property
    def monthly_amount(self):
        return self.annual_amount / Decimal("12")

    @property
    def estimated_fee(self):
        return (self.amount * self.fee_rate / Decimal("100")).quantize(CENT, rounding=ROUND_HALF_UP)

    def __str__(self):
        return f"{self.member} · {self.name}"


class FundDcaChange(models.Model):
    plan = models.ForeignKey(FundDcaPlan, on_delete=models.PROTECT, related_name="changes")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    action = models.CharField("操作", max_length=20)
    before = models.JSONField(default=dict)
    after = models.JSONField(default=dict)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "定投修改记录"
        verbose_name_plural = "定投修改记录"


class FundRefreshRun(TimestampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    family = models.ForeignKey(Family, on_delete=models.PROTECT, related_name="fund_refresh_runs")
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    is_scheduled = models.BooleanField("系统定时任务", default=False)
    status = models.CharField(max_length=20, default="queued")
    fund_ids = models.JSONField(default=list)
    message = models.CharField(max_length=500, blank=True)
    details = models.JSONField(default=list)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField()

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "基金数据更新任务"
        verbose_name_plural = "基金数据更新任务"
