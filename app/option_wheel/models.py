"""Auditable M1 foundation models for option wheel decision support."""

from datetime import datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import F, Q
from django.utils import timezone

from family_core.models import Family, TimestampedModel
from portfolio.models import (
    InvestmentAccount,
    InvestmentTransaction,
    OptionContract,
    Security,
)


class WheelAnalysisJob(TimestampedModel):
    """Mutable task status; financial evidence remains append-only and atomic."""

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    family = models.ForeignKey(Family, on_delete=models.PROTECT)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    selection = models.JSONField(default=dict)
    status = models.CharField(max_length=16, default="queued", choices=[
        ("queued", "等待启动"), ("running", "分析与清理中"),
        ("saved", "已保存"), ("failed", "未保存"), ("interrupted", "运行已中断"),
    ])
    message = models.TextField(blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField()
    decision_ids = models.JSONField(default=list)

    class Meta:
        ordering = ("-created_at",)
        constraints = [models.UniqueConstraint(
            fields=("family",), condition=Q(status__in=("queued", "running")),
            name="wheel_one_active_analysis_per_family",
        )]


class AppendOnlyQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("审计证据不允许批量更新。")

    def delete(self):
        raise ValidationError("审计证据不允许批量删除。")

    def bulk_create(self, objs, **kwargs):
        raise ValidationError("审计证据必须逐条校验后创建。")

    def bulk_update(self, objs, fields, **kwargs):
        raise ValidationError("审计证据不允许批量更新。")


class AppendOnlyManager(models.Manager.from_queryset(AppendOnlyQuerySet)):
    pass


class AppendOnlyEvidenceMixin:
    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            raise ValidationError("审计证据创建后不可修改。")
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("审计证据创建后不可删除。")


def _timestamp_within_age(as_of, reference, max_age_seconds):
    if not isinstance(as_of, datetime) or not isinstance(reference, datetime):
        return False
    try:
        age = reference - as_of
    except (TypeError, ValueError, OverflowError):
        return False
    return timedelta(0) <= age <= timedelta(seconds=max_age_seconds)


def _finite_decimal(value):
    return isinstance(value, Decimal) and value.is_finite()


def _decimal_from_json(value):
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except Exception:
        return None
    return parsed if parsed.is_finite() else None


class DataStatus(models.TextChoices):
    COMPLETE = "complete", "完整"
    PARTIAL = "partial", "部分"
    INVALID = "invalid", "无效"


class WheelCloseReport(AppendOnlyEvidenceMixin, TimestampedModel):
    """Independent observation evidence; never consumed by the live candidate engine."""

    family = models.ForeignKey(Family, on_delete=models.PROTECT)
    symbol = models.CharField(max_length=12)
    target_date = models.DateField()
    request_key = models.UUIDField()
    evidence = models.JSONField(default=dict)
    objects = AppendOnlyManager()

    class Meta:
        ordering = ("-created_at", "-pk")
        constraints = [models.UniqueConstraint(fields=("family", "request_key"), name="wheel_close_request_unique")]

    def clean(self):
        super().clean()
        if not isinstance(self.evidence, dict) or (
            self.evidence.get("mode") != "daily-close-observation-v1"
            or self.evidence.get("execution_allowed") is not False
            or self.evidence.get("symbol") != self.symbol
            or self.evidence.get("target_date") != str(self.target_date)
        ):
            raise ValidationError("收盘观察证据必须独立标识模式、日期和标的，且不得允许执行。")


class DelayStatus(models.TextChoices):
    REAL_TIME = "real_time", "实时"
    DELAYED = "delayed", "延迟"
    UNKNOWN = "unknown", "未知"


class Freshness(models.TextChoices):
    FRESH = "fresh", "新鲜"
    STALE = "stale", "过期"
    UNKNOWN = "unknown", "未知"


class EventStatus(models.TextChoices):
    CLEAR = "clear", "通过"
    BLOCKED = "blocked", "阻断"
    UNKNOWN = "unknown", "未知"


class TechnicalStatus(models.TextChoices):
    COMPLETE = "complete", "完整"
    PARTIAL = "partial", "部分"
    UNKNOWN = "unknown", "未知"


class OverallStatus(models.TextChoices):
    INVESTIGATION = "investigation", "调查中"
    BLOCKED = "blocked", "阻断"
    EXECUTABLE = "executable", "可执行"


class StandardStatus(models.TextChoices):
    STANDARD = "standard", "标准"
    NON_STANDARD = "non_standard", "非标准"
    UNKNOWN = "unknown", "未知"


class SettlementEvidence(models.TextChoices):
    PROVIDER_PHYSICAL = "provider_physical", "券商实物"
    OCC_STANDARD_EQUITY = "occ_standard_equity", "OCC 标准"
    UNKNOWN = "unknown", "未知"


class Strategy(models.TextChoices):
    SELL_PUT = "sell_put", "卖出看跌"
    COVERED_CALL = "covered_call", "备兑看涨"
    ROLL = "roll", "滚动"
    WAIT = "wait", "等待"


class CycleStatus(models.TextChoices):
    OPEN = "open", "进行中"
    PAUSED = "paused", "已暂停"
    CLOSED = "closed", "已结束"


class LegStatus(models.TextChoices):
    PLANNED = "planned", "计划中"
    OPEN = "open", "持仓中"
    CLOSED = "closed", "已平仓"
    ASSIGNED = "assigned", "已指派"
    EXPIRED = "expired", "已到期"
    CANCELLED = "cancelled", "已取消"


class WheelPolicy(TimestampedModel):
    family = models.ForeignKey(
        Family,
        verbose_name="所属家庭",
        on_delete=models.CASCADE,
        related_name="wheel_policies",
    )
    account = models.ForeignKey(
        InvestmentAccount,
        verbose_name="投资账户",
        on_delete=models.CASCADE,
        related_name="wheel_policies",
    )
    underlying = models.ForeignKey(
        Security,
        verbose_name="正股标的",
        on_delete=models.CASCADE,
        related_name="wheel_policies",
    )
    enabled = models.BooleanField("启用", default=True)
    preferred_premium_min = models.DecimalField(
        "最低权利金",
        max_digits=20,
        decimal_places=4,
        default=Decimal("90"),
        validators=[MinValueValidator(Decimal("0"))],
    )
    preferred_premium_max = models.DecimalField(
        "最高权利金",
        max_digits=20,
        decimal_places=4,
        default=Decimal("500"),
        validators=[MinValueValidator(Decimal("0"))],
    )
    preferred_dte_min = models.PositiveIntegerField(
        "最短到期天数",
        default=7,
        validators=[MinValueValidator(1)],
    )
    preferred_dte_max = models.PositiveIntegerField(
        "最长到期天数",
        default=30,
        validators=[MinValueValidator(1)],
    )
    max_underlying_nav_ratio = models.DecimalField(
        "最大标的 NAV 占比",
        max_digits=10,
        decimal_places=6,
        default=Decimal("1"),
        validators=[
            MinValueValidator(Decimal("0.000001")),
            MaxValueValidator(Decimal("1")),
        ],
    )
    max_spread_ratio = models.DecimalField(
        "最大买卖价差比例",
        max_digits=10,
        decimal_places=6,
        default=Decimal("1"),
        validators=[
            MinValueValidator(Decimal("0")),
            MaxValueValidator(Decimal("1")),
        ],
    )
    min_open_interest = models.PositiveIntegerField(
        "最小未平仓量",
        default=0,
        validators=[MinValueValidator(0)],
    )
    min_volume = models.PositiveIntegerField(
        "最小成交量",
        default=0,
        validators=[MinValueValidator(0)],
    )
    account_snapshot_max_age_minutes = models.PositiveIntegerField(
        "账户快照最大年龄（分钟）",
        default=1440,
        validators=[MinValueValidator(1)],
    )
    quote_max_age_seconds = models.PositiveIntegerField(
        "报价最大年龄（秒）",
        default=600,
        validators=[MinValueValidator(1)],
    )
    ruleset_version = models.CharField(
        "规则集版本",
        max_length=30,
        default="decision-v1",
    )

    class Meta:
        verbose_name = "期权轮动策略"
        verbose_name_plural = "期权轮动策略"
        constraints = [
            models.UniqueConstraint(
                fields=["family", "account", "underlying"],
                name="unique_wheel_policy_family_account_underlying",
            ),
            models.CheckConstraint(
                condition=(
                    Q(preferred_premium_min__gte=0)
                    & Q(
                        preferred_premium_max__gte=F(
                            "preferred_premium_min"
                        )
                    )
                ),
                name="wheel_policy_premium_order",
            ),
            models.CheckConstraint(
                condition=(
                    Q(preferred_dte_min__gte=1)
                    & Q(preferred_dte_max__gte=F("preferred_dte_min"))
                ),
                name="wheel_policy_dte_order",
            ),
            models.CheckConstraint(
                condition=(
                    Q(max_underlying_nav_ratio__gt=0)
                    & Q(max_underlying_nav_ratio__lte=1)
                ),
                name="wheel_policy_nav_ratio_range",
            ),
            models.CheckConstraint(
                condition=(
                    Q(max_spread_ratio__gte=0)
                    & Q(max_spread_ratio__lte=1)
                ),
                name="wheel_policy_spread_ratio_range",
            ),
            models.CheckConstraint(
                condition=Q(account_snapshot_max_age_minutes__gt=0),
                name="wheel_policy_account_age_positive",
            ),
            models.CheckConstraint(
                condition=Q(quote_max_age_seconds__gt=0),
                name="wheel_policy_quote_age_positive",
            ),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if self.account_id and self.family_id:
            if self.account.family_id != self.family_id:
                errors["account"] = "账户所属家庭与策略家庭不一致。"
        if self.underlying_id:
            if self.underlying.asset_type != Security.TYPE_STOCK:
                errors["underlying"] = "M1 仅接受股票类型标的。"
            if self.underlying.currency != "USD":
                errors["underlying"] = "M1 仅接受 USD 计价标的。"
            if self.underlying.market.strip().upper() != "US":
                errors["underlying"] = "M1 仅接受美国市场股票标的。"
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return (
            f"{self.family} {self.underlying.symbol} "
            f"{self.ruleset_version}"
        )


class WheelBrokerAccountSnapshot(
    AppendOnlyEvidenceMixin,
    TimestampedModel,
):
    objects = AppendOnlyManager()
    SOURCE_MANUAL_FILE = "manual_file"
    SOURCE_PORTFOLIO_READONLY = "portfolio_readonly"
    SOURCE_ZHIFU_READONLY = "zhifu_readonly"
    SOURCE_IBKR_READONLY = "ibkr_readonly"
    SOURCE_KIND_CHOICES = [
        (SOURCE_MANUAL_FILE, "手工文件"),
        (SOURCE_PORTFOLIO_READONLY, "投资组合只读"),
        (SOURCE_ZHIFU_READONLY, "致富只读"),
        (SOURCE_IBKR_READONLY, "IBKR 只读"),
    ]

    family = models.ForeignKey(
        Family,
        verbose_name="所属家庭",
        on_delete=models.PROTECT,
        related_name="wheel_broker_snapshots",
    )
    account = models.ForeignKey(
        InvestmentAccount,
        verbose_name="投资账户",
        on_delete=models.PROTECT,
        related_name="wheel_broker_snapshots",
    )
    source_kind = models.CharField(
        "来源类型",
        max_length=30,
        choices=SOURCE_KIND_CHOICES,
    )
    source_reference = models.CharField(
        "来源引用",
        max_length=200,
        blank=True,
    )
    currency = models.CharField("币种", max_length=10, default="USD")
    settled_cash = models.DecimalField(
        "已结算现金",
        max_digits=24,
        decimal_places=4,
        null=True,
        blank=True,
    )
    unsettled_cash = models.DecimalField(
        "未结算现金",
        max_digits=24,
        decimal_places=4,
        null=True,
        blank=True,
    )
    nav = models.DecimalField(
        "净值",
        max_digits=24,
        decimal_places=4,
        null=True,
        blank=True,
    )
    reserved_cash = models.DecimalField(
        "预留现金",
        max_digits=24,
        decimal_places=4,
        null=True,
        blank=True,
    )
    margin_loan_balance = models.DecimalField(
        "保证金贷款余额",
        max_digits=24,
        decimal_places=4,
        null=True,
        blank=True,
    )
    uses_margin = models.BooleanField(
        "使用保证金",
        null=True,
        blank=True,
        default=None,
    )
    positions_summary = models.JSONField(
        "持仓摘要",
        null=True,
        blank=True,
    )
    open_obligations = models.JSONField(
        "未了义务",
        null=True,
        blank=True,
    )
    source_as_of = models.DateTimeField("来源时点")
    fetched_at = models.DateTimeField(
        "获取时间",
        default=timezone.now,
    )
    data_status = models.CharField(
        "数据状态",
        max_length=20,
        choices=DataStatus.choices,
        default=DataStatus.PARTIAL,
    )

    class Meta:
        verbose_name = "券商账户快照"
        verbose_name_plural = "券商账户快照"
        base_manager_name = "objects"
        default_manager_name = "objects"
        indexes = [
            models.Index(
                fields=["family", "account", "source_as_of"],
                name="wheel_broker_acct_asof_idx",
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["family", "account", "source_kind", "source_reference"],
                condition=~Q(source_reference=""),
                name="wheel_broker_snapshot_source_unique",
            ),
            models.CheckConstraint(
                condition=(
                    (Q(settled_cash__isnull=True) | Q(settled_cash__gte=0))
                    & (
                        Q(unsettled_cash__isnull=True)
                        | Q(unsettled_cash__gte=0)
                    )
                    & (Q(nav__isnull=True) | Q(nav__gte=0))
                    & (
                        Q(reserved_cash__isnull=True)
                        | Q(reserved_cash__gte=0)
                    )
                    & (
                        Q(margin_loan_balance__isnull=True)
                        | Q(margin_loan_balance__gte=0)
                    )
                ),
                name="wheel_broker_non_negative_amounts",
            ),
        ]

    def clean(self):
        super().clean()
        if self.account_id and self.family_id:
            if self.account.family_id != self.family_id:
                raise ValidationError(
                    {"account": "账户所属家庭与快照家庭不一致。"}
                )

    def __str__(self):
        return (
            f"{self.family} {self.account} "
            f"{self.source_as_of:%Y-%m-%d %H:%M}"
        )


class WheelMarketSnapshot(AppendOnlyEvidenceMixin, models.Model):
    objects = AppendOnlyManager()
    underlying = models.ForeignKey(
        Security,
        verbose_name="正股标的",
        on_delete=models.PROTECT,
        related_name="wheel_market_snapshots",
    )
    provider = models.CharField("行情来源", max_length=50)
    provider_symbol = models.CharField(
        "行情源代码",
        max_length=100,
        blank=True,
    )
    last_price = models.DecimalField(
        "最新价",
        max_digits=20,
        decimal_places=6,
        null=True,
        blank=True,
    )
    source_as_of = models.DateTimeField("来源时点")
    fetched_at = models.DateTimeField(
        "获取时间",
        default=timezone.now,
    )
    market_session = models.CharField(
        "市场会话",
        max_length=50,
        blank=True,
    )
    regular_session_verified = models.BooleanField(
        "正常交易时段已核验",
        default=False,
    )
    calendar_reference = models.CharField(
        "交易日历证据",
        max_length=100,
        blank=True,
    )
    delay_status = models.CharField(
        "延迟状态",
        max_length=20,
        choices=DelayStatus.choices,
        default=DelayStatus.UNKNOWN,
    )
    freshness_status = models.CharField(
        "新鲜度",
        max_length=20,
        choices=Freshness.choices,
        default=Freshness.UNKNOWN,
    )
    data_quality = models.CharField(
        "数据质量",
        max_length=20,
        choices=DataStatus.choices,
        default=DataStatus.PARTIAL,
    )
    sanitized_metadata = models.JSONField(
        "脱敏元数据",
        default=dict,
        blank=True,
    )

    class Meta:
        verbose_name = "轮动行情快照"
        verbose_name_plural = "轮动行情快照"
        base_manager_name = "objects"
        default_manager_name = "objects"
        indexes = [
            models.Index(
                fields=["underlying", "source_as_of"],
                name="wheel_mkt_sec_asof_idx",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(last_price__isnull=True) | Q(last_price__gt=0)
                ),
                name="wheel_mkt_last_price_positive",
            ),
        ]

    def __str__(self):
        return (
            f"{self.underlying.symbol} "
            f"{self.source_as_of:%Y-%m-%d %H:%M}"
        )


class WheelOptionQuoteSnapshot(AppendOnlyEvidenceMixin, models.Model):
    objects = AppendOnlyManager()
    PUT = "put"
    CALL = "call"
    OPTION_TYPE_CHOICES = [(PUT, "看跌"), (CALL, "看涨")]

    underlying = models.ForeignKey(
        Security,
        verbose_name="正股标的",
        on_delete=models.PROTECT,
        related_name="wheel_option_quotes",
    )
    market_snapshot = models.ForeignKey(
        WheelMarketSnapshot,
        verbose_name="关联行情快照",
        on_delete=models.PROTECT,
        related_name="option_quotes",
    )
    provider = models.CharField("行情来源", max_length=50)
    provider_contract_code = models.CharField(
        "合约代码",
        max_length=100,
    )
    currency = models.CharField("报价币种", max_length=10, blank=True)
    option_type = models.CharField(
        "期权类型",
        max_length=10,
        choices=OPTION_TYPE_CHOICES,
    )
    expiration = models.DateField("到期日")
    strike = models.DecimalField(
        "行权价",
        max_digits=20,
        decimal_places=6,
    )
    standard_status = models.CharField(
        "标准状态",
        max_length=20,
        choices=StandardStatus.choices,
        default=StandardStatus.UNKNOWN,
    )
    is_adjusted = models.BooleanField(
        "是否调整",
        null=True,
        blank=True,
        default=None,
    )
    index_option_type = models.CharField(
        "指数期权类型",
        max_length=30,
        blank=True,
    )
    underlying_asset_type = models.CharField(
        "标的资产类型",
        max_length=30,
        blank=True,
    )
    exercise_style = models.CharField(
        "行权方式",
        max_length=20,
        blank=True,
    )
    settlement_mode = models.CharField(
        "结算方式",
        max_length=30,
        blank=True,
    )
    settlement_evidence = models.CharField(
        "结算证据",
        max_length=30,
        choices=SettlementEvidence.choices,
        default=SettlementEvidence.UNKNOWN,
    )
    deliverable_shares = models.PositiveIntegerField(
        "可交割股数",
        null=True,
        blank=True,
    )
    contract_multiplier = models.PositiveIntegerField(
        "合约乘数",
        null=True,
        blank=True,
    )
    bid = models.DecimalField(
        "买价",
        max_digits=20,
        decimal_places=6,
        null=True,
        blank=True,
    )
    ask = models.DecimalField(
        "卖价",
        max_digits=20,
        decimal_places=6,
        null=True,
        blank=True,
    )
    bid_size = models.PositiveBigIntegerField(
        "买量",
        null=True,
        blank=True,
    )
    ask_size = models.PositiveBigIntegerField(
        "卖量",
        null=True,
        blank=True,
    )
    last = models.DecimalField(
        "最新价",
        max_digits=20,
        decimal_places=6,
        null=True,
        blank=True,
    )
    volume = models.PositiveBigIntegerField(
        "成交量",
        null=True,
        blank=True,
    )
    open_interest = models.PositiveBigIntegerField(
        "未平仓量",
        null=True,
        blank=True,
    )
    implied_volatility = models.DecimalField(
        "隐含波动率",
        max_digits=12,
        decimal_places=6,
        null=True,
        blank=True,
    )
    delta = models.DecimalField(
        "Delta",
        max_digits=12,
        decimal_places=8,
        null=True,
        blank=True,
    )
    gamma = models.DecimalField(
        "Gamma",
        max_digits=12,
        decimal_places=8,
        null=True,
        blank=True,
    )
    theta = models.DecimalField(
        "Theta",
        max_digits=12,
        decimal_places=8,
        null=True,
        blank=True,
    )
    vega = models.DecimalField(
        "Vega",
        max_digits=12,
        decimal_places=8,
        null=True,
        blank=True,
    )
    rho = models.DecimalField(
        "Rho",
        max_digits=12,
        decimal_places=8,
        null=True,
        blank=True,
    )
    assignment_probability = models.DecimalField(
        "指派概率（%）",
        max_digits=7,
        decimal_places=4,
        null=True,
        blank=True,
    )
    quote_as_of = models.DateTimeField("报价时点")
    fetched_at = models.DateTimeField(
        "获取时间",
        default=timezone.now,
    )
    delay_status = models.CharField(
        "延迟状态",
        max_length=20,
        choices=DelayStatus.choices,
        default=DelayStatus.UNKNOWN,
    )
    freshness_status = models.CharField(
        "新鲜度",
        max_length=20,
        choices=Freshness.choices,
        default=Freshness.UNKNOWN,
    )
    data_quality = models.CharField(
        "数据质量",
        max_length=20,
        choices=DataStatus.choices,
        default=DataStatus.PARTIAL,
    )
    sanitized_metadata = models.JSONField(
        "脱敏元数据",
        default=dict,
        blank=True,
    )

    class Meta:
        verbose_name = "期权报价快照"
        verbose_name_plural = "期权报价快照"
        base_manager_name = "objects"
        default_manager_name = "objects"
        indexes = [
            models.Index(
                fields=["underlying", "expiration", "strike"],
                name="wheel_opt_sec_exp_stk_idx",
            ),
            models.Index(
                fields=["provider", "provider_contract_code"],
                name="wheel_opt_prov_code_idx",
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "provider",
                    "provider_contract_code",
                    "quote_as_of",
                ],
                name="unique_wheel_opt_prov_code_asof",
            ),
            models.CheckConstraint(
                condition=Q(strike__gt=0),
                name="wheel_opt_strike_positive",
            ),
            models.CheckConstraint(
                condition=(
                    Q(deliverable_shares__isnull=True)
                    | Q(deliverable_shares__gt=0)
                ),
                name="wheel_opt_deliverable_positive",
            ),
            models.CheckConstraint(
                condition=(
                    Q(contract_multiplier__isnull=True)
                    | Q(contract_multiplier__gt=0)
                ),
                name="wheel_opt_multiplier_positive",
            ),
            models.CheckConstraint(
                condition=Q(bid__isnull=True) | Q(bid__gte=0),
                name="wheel_opt_bid_non_negative",
            ),
            models.CheckConstraint(
                condition=Q(ask__isnull=True) | Q(ask__gte=0),
                name="wheel_opt_ask_non_negative",
            ),
            models.CheckConstraint(
                condition=(
                    Q(bid__isnull=True)
                    | Q(ask__isnull=True)
                    | Q(ask__gte=F("bid"))
                ),
                name="wheel_opt_ask_ge_bid",
            ),
            models.CheckConstraint(
                condition=(
                    Q(assignment_probability__isnull=True)
                    | (
                        Q(assignment_probability__gte=0)
                        & Q(assignment_probability__lte=100)
                    )
                ),
                name="wheel_opt_prob_range",
            ),
        ]

    def clean(self):
        super().clean()
        if self.market_snapshot_id and self.underlying_id:
            if self.market_snapshot.underlying_id != self.underlying_id:
                raise ValidationError(
                    {
                        "market_snapshot": (
                            "行情快照标的与期权标的不一致。"
                        )
                    }
                )

    def __str__(self):
        return (
            f"{self.underlying.symbol} "
            f"{self.get_option_type_display()} "
            f"{self.expiration} {self.strike}"
        )


class WheelDecision(AppendOnlyEvidenceMixin, TimestampedModel):
    objects = AppendOnlyManager()
    family = models.ForeignKey(
        Family,
        verbose_name="所属家庭",
        on_delete=models.PROTECT,
        related_name="wheel_decisions",
    )
    account = models.ForeignKey(
        InvestmentAccount,
        verbose_name="投资账户",
        on_delete=models.PROTECT,
        related_name="wheel_decisions",
    )
    underlying = models.ForeignKey(
        Security,
        verbose_name="正股标的",
        on_delete=models.PROTECT,
        related_name="wheel_decisions",
    )
    policy = models.ForeignKey(
        WheelPolicy,
        verbose_name="策略",
        on_delete=models.PROTECT,
        related_name="decisions",
    )
    technical_snapshot = models.ForeignKey(
        "WheelTechnicalSnapshot", verbose_name="技术快照",
        on_delete=models.PROTECT, related_name="decisions",
        null=True, blank=True,
    )
    event_snapshot = models.ForeignKey(
        "WheelEventSnapshot", verbose_name="事件快照",
        on_delete=models.PROTECT, related_name="decisions",
        null=True, blank=True,
    )
    market_regime_snapshot = models.ForeignKey(
        "WheelMarketRegimeSnapshot", verbose_name="市场环境快照",
        on_delete=models.PROTECT, related_name="decisions",
        null=True, blank=True,
    )
    account_snapshot = models.ForeignKey(
        WheelBrokerAccountSnapshot,
        verbose_name="账户快照",
        on_delete=models.PROTECT,
        related_name="decisions",
    )
    market_snapshot = models.ForeignKey(
        WheelMarketSnapshot,
        verbose_name="行情快照",
        on_delete=models.PROTECT,
        related_name="decisions",
    )
    decision_time = models.DateTimeField(
        "决策时间",
        default=timezone.now,
    )
    input_fingerprint = models.CharField("输入指纹", max_length=64)
    ruleset_version = models.CharField(
        "规则集版本",
        max_length=30,
        default="m1-v1",
    )
    event_status = models.CharField(
        "事件状态",
        max_length=20,
        choices=EventStatus.choices,
        default=EventStatus.UNKNOWN,
    )
    technical_status = models.CharField(
        "技术状态",
        max_length=20,
        choices=TechnicalStatus.choices,
        default=TechnicalStatus.UNKNOWN,
    )
    execution_gate_open = models.BooleanField(
        "执行门禁已打开",
        default=False,
    )
    overall_status = models.CharField(
        "总体状态",
        max_length=20,
        choices=OverallStatus.choices,
        default=OverallStatus.INVESTIGATION,
    )
    blockers = models.JSONField("阻断项", default=list, blank=True)
    frozen_input = models.JSONField(
        "冻结输入",
        default=dict,
        blank=True,
    )

    class Meta:
        verbose_name = "轮动决策"
        verbose_name_plural = "轮动决策"
        base_manager_name = "objects"
        default_manager_name = "objects"
        indexes = [
            models.Index(
                fields=["family", "underlying", "decision_time"],
                name="wheel_dec_fam_sec_time_idx",
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "family",
                    "account",
                    "underlying",
                    "input_fingerprint",
                ],
                name="unique_wheel_dec_fam_acct_fp",
            ),
            models.CheckConstraint(
                condition=(
                    ~Q(overall_status=OverallStatus.EXECUTABLE)
                    | Q(
                        execution_gate_open=True,
                        event_status=EventStatus.CLEAR,
                        technical_status=TechnicalStatus.COMPLETE,
                    )
                ),
                name="wheel_dec_executable_gate",
            ),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if self.policy_id:
            if (
                self.policy.family_id != self.family_id
                or self.policy.account_id != self.account_id
                or self.policy.underlying_id != self.underlying_id
            ):
                errors["policy"] = (
                    "策略与决策的家庭、账户或标的不一致。"
                )
        if self.account_snapshot_id:
            if (
                self.account_snapshot.family_id != self.family_id
                or self.account_snapshot.account_id != self.account_id
            ):
                errors["account_snapshot"] = (
                    "账户快照与决策的家庭或账户不一致。"
                )
        if self.market_snapshot_id:
            if self.market_snapshot.underlying_id != self.underlying_id:
                errors["market_snapshot"] = (
                    "行情快照标的与决策标的不一致。"
                )
        if self.technical_snapshot_id and self.technical_snapshot.underlying_id != self.underlying_id:
            errors["technical_snapshot"] = "技术快照标的与决策标的不一致。"
        if self.event_snapshot_id and self.event_snapshot.underlying_id != self.underlying_id:
            errors["event_snapshot"] = "事件快照标的与决策标的不一致。"
        if self.overall_status == OverallStatus.EXECUTABLE:
            policy = self.policy if self.policy_id else None
            if not settings.OPTION_WHEEL_EXECUTION_ENABLED:
                errors["execution_gate_open"] = (
                    "M1 验收完成前全局执行门禁保持关闭。"
                )
            if not self.execution_gate_open:
                errors["execution_gate_open"] = (
                    "可执行决策要求显式打开执行门禁。"
                )
            if policy is None or not policy.enabled:
                errors["policy"] = "可执行决策要求策略已启用。"
            if self.event_status != EventStatus.CLEAR:
                errors["event_status"] = (
                    "可执行决策要求事件状态为通过。"
                )
            if self.technical_status != TechnicalStatus.COMPLETE:
                errors["technical_status"] = (
                    "可执行决策要求技术状态为完整。"
                )
            existing_exposure = (
                _decimal_from_json(
                    self.frozen_input.get(
                        "already_exposed_notional"
                    )
                )
                if isinstance(self.frozen_input, dict)
                else None
            )
            if (
                existing_exposure is None
                or existing_exposure < 0
                or self.frozen_input.get(
                    "account_identity_verified"
                )
                is not True
                or self.frozen_input.get("event_evidence_verified")
                is not True
                or self.frozen_input.get(
                    "technical_evidence_verified"
                )
                is not True
            ):
                errors["frozen_input"] = (
                    "可执行决策要求冻结账户、事件和技术证据。"
                )
            account_snapshot = (
                self.account_snapshot if self.account_snapshot_id else None
            )
            if (
                account_snapshot is None
                or account_snapshot.data_status != DataStatus.COMPLETE
            ):
                errors["account_snapshot"] = (
                    "可执行决策要求账户快照完整。"
                )
            elif (
                account_snapshot.currency != "USD"
                or not account_snapshot.source_reference.strip()
                or not _finite_decimal(account_snapshot.settled_cash)
                or not _finite_decimal(account_snapshot.unsettled_cash)
                or not _finite_decimal(account_snapshot.nav)
                or account_snapshot.nav <= 0
                or not _finite_decimal(account_snapshot.reserved_cash)
                or account_snapshot.reserved_cash
                > account_snapshot.settled_cash
                or account_snapshot.uses_margin is not False
                or not _finite_decimal(
                    account_snapshot.margin_loan_balance
                )
                or account_snapshot.margin_loan_balance != 0
                or not isinstance(account_snapshot.positions_summary, dict)
                or not isinstance(account_snapshot.open_obligations, dict)
                or policy is None
                or not _timestamp_within_age(
                    account_snapshot.source_as_of,
                    self.decision_time,
                    policy.account_snapshot_max_age_minutes * 60,
                )
            ):
                errors["account_snapshot"] = (
                    "可执行决策要求完整 USD 现金证据且禁止融资。"
                )
            market_snapshot = (
                self.market_snapshot if self.market_snapshot_id else None
            )
            if (
                market_snapshot is None
                or market_snapshot.data_quality != DataStatus.COMPLETE
                or market_snapshot.delay_status != DelayStatus.REAL_TIME
                or market_snapshot.freshness_status != Freshness.FRESH
                or not _finite_decimal(market_snapshot.last_price)
                or market_snapshot.last_price <= 0
                or market_snapshot.market_session.strip().lower()
                != "regular"
                or market_snapshot.regular_session_verified is not True
                or not market_snapshot.calendar_reference.strip()
                or market_snapshot.source_as_of.weekday() >= 5
                or policy is None
                or not _timestamp_within_age(
                    market_snapshot.source_as_of,
                    self.decision_time,
                    policy.quote_max_age_seconds,
                )
            ):
                errors["market_snapshot"] = (
                    "可执行决策要求完整、实时且新鲜的行情。"
                )
            if self.blockers:
                errors["blockers"] = "可执行决策要求无阻断项。"
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return (
            f"{self.family} {self.underlying.symbol} "
            f"{self.decision_time:%Y-%m-%d %H:%M}"
        )


class WheelCandidate(AppendOnlyEvidenceMixin, TimestampedModel):
    objects = AppendOnlyManager()
    decision = models.ForeignKey(
        WheelDecision,
        verbose_name="决策",
        on_delete=models.PROTECT,
        related_name="candidates",
    )
    option_quote = models.ForeignKey(
        WheelOptionQuoteSnapshot,
        verbose_name="期权报价",
        on_delete=models.PROTECT,
        related_name="candidates",
        null=True,
        blank=True,
    )
    candidate_key = models.CharField("候选键", max_length=100)
    strategy = models.CharField(
        "策略",
        max_length=20,
        choices=Strategy.choices,
    )
    status = models.CharField(
        "状态",
        max_length=20,
        choices=OverallStatus.choices,
        default=OverallStatus.INVESTIGATION,
    )
    contract_count = models.PositiveIntegerField(
        "合约数量",
        default=1,
    )
    required_cash = models.DecimalField(
        "所需现金",
        max_digits=24,
        decimal_places=4,
        null=True,
        blank=True,
    )
    premium_total = models.DecimalField(
        "权利金总额",
        max_digits=24,
        decimal_places=4,
        null=True,
        blank=True,
    )
    break_even = models.DecimalField(
        "盈亏平衡价",
        max_digits=20,
        decimal_places=6,
        null=True,
        blank=True,
    )
    annualized_premium_rate = models.DecimalField(
        "年化权利金率",
        max_digits=12,
        decimal_places=8,
        null=True,
        blank=True,
    )
    assignment_probability = models.DecimalField(
        "指派概率（%）",
        max_digits=7,
        decimal_places=4,
        null=True,
        blank=True,
    )
    premium_preference_match = models.BooleanField(
        "权利金偏好匹配",
        default=False,
    )
    dte_preference_match = models.BooleanField(
        "到期日偏好匹配",
        default=False,
    )
    exclusion_reasons = models.JSONField(
        "排除原因",
        default=list,
        blank=True,
    )
    warning_reasons = models.JSONField(
        "风险提示",
        default=list,
        blank=True,
    )
    calculation_details = models.JSONField(
        "计算详情",
        default=dict,
        blank=True,
    )

    class Meta:
        verbose_name = "轮动候选"
        verbose_name_plural = "轮动候选"
        base_manager_name = "objects"
        default_manager_name = "objects"
        indexes = [
            models.Index(
                fields=["decision", "status"],
                name="wheel_cand_dec_status_idx",
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["decision", "candidate_key"],
                name="unique_wheel_cand_dec_key",
            ),
            models.CheckConstraint(
                condition=(
                    Q(assignment_probability__isnull=True)
                    | (
                        Q(assignment_probability__gte=0)
                        & Q(assignment_probability__lte=100)
                    )
                ),
                name="wheel_cand_prob_range",
            ),
            models.CheckConstraint(
                condition=(
                    ~Q(status=OverallStatus.EXECUTABLE)
                    | Q(
                        strategy=Strategy.SELL_PUT,
                        option_quote__isnull=False,
                        contract_count__gte=1,
                        required_cash__gt=0,
                        premium_total__gt=0,
                        break_even__gte=0,
                        annualized_premium_rate__gt=0,
                        assignment_probability__isnull=False,
                    )
                ),
                name="wheel_cand_executable_fields",
            ),
        ]

    def clean(self):
        super().clean()
        errors = {}
        is_wait = self.strategy == Strategy.WAIT
        if not is_wait:
            if self.option_quote_id is None:
                errors["option_quote"] = (
                    "非等待策略必须关联报价。"
                )
            if self.contract_count < 1:
                errors["contract_count"] = (
                    "非等待策略合约数至少为 1。"
                )
        if self.option_quote_id and self.decision_id:
            if (
                self.option_quote.underlying_id
                != self.decision.underlying_id
            ):
                errors["option_quote"] = (
                    "报价标的与决策标的不一致。"
                )
        if self.status == OverallStatus.EXECUTABLE:
            if not settings.OPTION_WHEEL_EXECUTION_ENABLED:
                errors["status"] = (
                    "M1 验收完成前禁止持久化可执行候选。"
                )
            if (
                not self.decision_id
                or self.decision.overall_status
                != OverallStatus.EXECUTABLE
            ):
                errors["status"] = "可执行候选要求决策可执行。"
            if self.strategy != Strategy.SELL_PUT:
                errors["status"] = (
                    "M1 当前只允许卖出看跌候选标记为可执行。"
                )
            if self.exclusion_reasons:
                errors["exclusion_reasons"] = (
                    "可执行候选要求无排除原因。"
                )
            for field_name in (
                "required_cash",
                "premium_total",
                "break_even",
                "annualized_premium_rate",
                "assignment_probability",
            ):
                if getattr(self, field_name) is None:
                    errors[field_name] = (
                        "可执行候选要求计算值非空。"
                    )
            if self.option_quote_id:
                quote = self.option_quote
                policy = (
                    self.decision.policy if self.decision_id else None
                )
                if (
                    quote.data_quality != DataStatus.COMPLETE
                    or quote.delay_status != DelayStatus.REAL_TIME
                    or quote.freshness_status != Freshness.FRESH
                    or quote.currency != "USD"
                    or policy is None
                    or not _timestamp_within_age(
                        quote.quote_as_of,
                        self.decision.decision_time,
                        policy.quote_max_age_seconds,
                    )
                ):
                    errors["option_quote"] = (
                        "可执行候选要求完整、实时且新鲜的报价。"
                    )
                option_type_ok = (
                    self.strategy != Strategy.SELL_PUT
                    or quote.option_type == WheelOptionQuoteSnapshot.PUT
                ) and (
                    self.strategy != Strategy.COVERED_CALL
                    or quote.option_type == WheelOptionQuoteSnapshot.CALL
                )
                identity_ok = (
                    option_type_ok
                    and quote.standard_status == StandardStatus.STANDARD
                    and quote.is_adjusted is False
                    and quote.index_option_type.strip().upper() == "N/A"
                    and quote.underlying.asset_type == Security.TYPE_STOCK
                    and quote.underlying.market.strip().upper() == "US"
                    and quote.underlying_asset_type.strip().lower()
                    == Security.TYPE_STOCK
                    and quote.exercise_style.strip().upper() == "AMERICAN"
                    and quote.deliverable_shares == 100
                    and quote.contract_multiplier == 100
                )
                mode = quote.settlement_mode.strip().upper()
                provider_physical = (
                    mode == "PHYSICAL"
                    and quote.settlement_evidence
                    == SettlementEvidence.PROVIDER_PHYSICAL
                )
                occ_fallback = (
                    mode in {"", "N/A", "UNKNOWN"}
                    and quote.settlement_evidence
                    == SettlementEvidence.OCC_STANDARD_EQUITY
                    and identity_ok
                )
                if not identity_ok or not (
                    provider_physical or occ_fallback
                ):
                    errors["option_quote"] = (
                        "可执行候选要求标准、未调整、实物交割的美股期权证据。"
                    )
                quote_values_ok = (
                    identity_ok
                    and _finite_decimal(quote.strike)
                    and quote.strike > 0
                    and _finite_decimal(quote.bid)
                    and quote.bid > 0
                    and _finite_decimal(quote.ask)
                    and quote.ask >= quote.bid
                    and policy is not None
                    and _finite_decimal(quote.assignment_probability)
                    and Decimal("0")
                    <= quote.assignment_probability
                    <= Decimal("100")
                )
                if not quote_values_ok:
                    errors["option_quote"] = (
                        "可执行候选要求有效报价和指派概率。"
                    )

                dte = (
                    quote.expiration - self.decision.decision_time.date()
                ).days
                calculations_ok = (
                    quote_values_ok
                    and dte > 0
                    and _finite_decimal(self.required_cash)
                    and _finite_decimal(self.premium_total)
                    and _finite_decimal(self.break_even)
                    and _finite_decimal(self.annualized_premium_rate)
                    and _finite_decimal(self.assignment_probability)
                )
                if calculations_ok:
                    shares = Decimal(quote.deliverable_shares)
                    count = Decimal(self.contract_count)
                    expected_cash = quote.strike * shares * count
                    expected_premium = quote.bid * shares * count
                    expected_break_even = quote.strike - quote.bid
                    expected_annualized = (
                        expected_premium
                        / expected_cash
                        * Decimal("365")
                        / Decimal(dte)
                    )
                    calculations_ok = (
                        self.required_cash == expected_cash
                        and self.premium_total == expected_premium
                        and self.break_even == expected_break_even
                        and abs(
                            self.annualized_premium_rate
                            - expected_annualized
                        )
                        <= Decimal("0.00000001")
                        and self.assignment_probability
                        == quote.assignment_probability
                    )
                if not calculations_ok:
                    errors["calculation_details"] = (
                        "可执行候选的计算值必须由冻结报价一致重算。"
                    )
                existing_exposure = _decimal_from_json(
                    self.decision.frozen_input.get(
                        "already_exposed_notional"
                    )
                )
                account_snapshot = self.decision.account_snapshot
                capacity_ok = (
                    calculations_ok
                    and existing_exposure is not None
                    and existing_exposure >= 0
                    and _finite_decimal(account_snapshot.settled_cash)
                    and _finite_decimal(account_snapshot.reserved_cash)
                    and _finite_decimal(account_snapshot.nav)
                    and self.required_cash
                    <= account_snapshot.settled_cash
                    - account_snapshot.reserved_cash
                    and existing_exposure + self.required_cash
                    <= account_snapshot.nav
                    * self.decision.policy.max_underlying_nav_ratio
                )
                if not capacity_ok:
                    errors["required_cash"] = (
                        "可执行候选必须满足未占用现金和 NAV 暴露上限。"
                    )
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return f"{self.candidate_key} {self.get_strategy_display()}"


class WheelPause(TimestampedModel):
    family = models.ForeignKey(Family, on_delete=models.CASCADE, related_name="wheel_pauses")
    account = models.ForeignKey(
        InvestmentAccount, on_delete=models.CASCADE, related_name="wheel_pauses",
        null=True, blank=True,
    )
    underlying = models.ForeignKey(
        Security, on_delete=models.CASCADE, related_name="wheel_pauses",
        null=True, blank=True,
    )
    starts_at = models.DateTimeField("暂停开始", default=timezone.now)
    ends_at = models.DateTimeField("暂停结束", null=True, blank=True)
    reason = models.TextField("暂停原因")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )

    class Meta:
        verbose_name = "车轮暂停"
        verbose_name_plural = "车轮暂停"
        indexes = [models.Index(fields=["family", "starts_at", "ends_at"], name="wheel_pause_scope_idx")]

    def clean(self):
        errors = {}
        if self.ends_at and self.ends_at <= self.starts_at:
            errors["ends_at"] = "暂停结束时间必须晚于开始时间。"
        if self.account_id and self.account.family_id != self.family_id:
            errors["account"] = "账户与暂停范围不属于同一家庭。"
        if self.underlying_id and self.underlying.asset_type != Security.TYPE_STOCK:
            errors["underlying"] = "暂停标的必须是股票。"
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def is_active_at(self, moment=None):
        moment = moment or timezone.now()
        return self.starts_at <= moment and (self.ends_at is None or moment < self.ends_at)


class WheelTechnicalSnapshot(AppendOnlyEvidenceMixin, models.Model):
    objects = AppendOnlyManager()
    underlying = models.ForeignKey(Security, on_delete=models.PROTECT, related_name="wheel_technical_snapshots")
    provider = models.CharField("来源", max_length=50)
    source_as_of = models.DateTimeField("来源时点")
    fetched_at = models.DateTimeField("获取时间", default=timezone.now)
    sample_count = models.PositiveIntegerField("样本数", default=0)
    sma_20 = models.DecimalField("20 日均线", max_digits=20, decimal_places=6, null=True, blank=True)
    sma_50 = models.DecimalField("50 日均线", max_digits=20, decimal_places=6, null=True, blank=True)
    rsi_14 = models.DecimalField("14 日 RSI", max_digits=12, decimal_places=6, null=True, blank=True)
    atr_14 = models.DecimalField("14 日 ATR", max_digits=20, decimal_places=6, null=True, blank=True)
    return_5d = models.DecimalField("5 日收益率", max_digits=12, decimal_places=8, null=True, blank=True)
    return_20d = models.DecimalField("20 日收益率", max_digits=12, decimal_places=8, null=True, blank=True)
    status = models.CharField("状态", max_length=20, choices=TechnicalStatus.choices, default=TechnicalStatus.UNKNOWN)
    raw_evidence = models.JSONField("原始证据", default=dict, blank=True)

    class Meta:
        verbose_name = "技术分析快照"
        verbose_name_plural = "技术分析快照"
        indexes = [models.Index(fields=["underlying", "-source_as_of"], name="wheel_tech_under_asof_idx")]

    def clean(self):
        if self.underlying_id and self.underlying.asset_type != Security.TYPE_STOCK:
            raise ValidationError({"underlying": "技术快照标的必须是股票。"})
        if self.status == TechnicalStatus.COMPLETE:
            required = (self.sma_20, self.sma_50, self.rsi_14, self.atr_14, self.return_5d, self.return_20d)
            if self.sample_count < 50 or any(not _finite_decimal(v) for v in required):
                raise ValidationError({"status": "完整技术快照至少需要 50 个样本及全部指标。"})


class WheelEventSnapshot(AppendOnlyEvidenceMixin, models.Model):
    objects = AppendOnlyManager()
    underlying = models.ForeignKey(Security, on_delete=models.PROTECT, related_name="wheel_event_snapshots")
    provider = models.CharField("来源", max_length=50)
    window_start = models.DateField("窗口开始")
    window_end = models.DateField("窗口结束")
    earnings_status = models.CharField("财报状态", max_length=20, choices=EventStatus.choices, default=EventStatus.UNKNOWN)
    earnings_at = models.DateTimeField("财报时间", null=True, blank=True)
    dividend_status = models.CharField("除息状态", max_length=20, choices=EventStatus.choices, default=EventStatus.UNKNOWN)
    ex_dividend_date = models.DateField("除息日", null=True, blank=True)
    source_as_of = models.DateTimeField("来源时点")
    fetched_at = models.DateTimeField("获取时间", default=timezone.now)
    raw_evidence = models.JSONField("原始证据", default=dict, blank=True)

    class Meta:
        verbose_name = "事件快照"
        verbose_name_plural = "事件快照"
        indexes = [models.Index(fields=["underlying", "-source_as_of"], name="wheel_event_under_asof_idx")]

    @property
    def overall_status(self):
        statuses = {self.earnings_status, self.dividend_status}
        if EventStatus.BLOCKED in statuses:
            return EventStatus.BLOCKED
        if statuses == {EventStatus.CLEAR}:
            return EventStatus.CLEAR
        return EventStatus.UNKNOWN

    def clean(self):
        if self.window_end < self.window_start:
            raise ValidationError({"window_end": "事件窗口结束日不得早于开始日。"})


class WheelMarketRegimeSnapshot(AppendOnlyEvidenceMixin, models.Model):
    objects = AppendOnlyManager()
    provider = models.CharField("来源", max_length=50)
    regime = models.CharField("市场状态", max_length=30)
    source_as_of = models.DateTimeField("来源时点")
    fetched_at = models.DateTimeField("获取时间", default=timezone.now)
    status = models.CharField("数据状态", max_length=20, choices=DataStatus.choices, default=DataStatus.PARTIAL)
    vix = models.DecimalField("VIX", max_digits=12, decimal_places=6, null=True, blank=True)
    raw_evidence = models.JSONField("原始证据", default=dict, blank=True)

    class Meta:
        verbose_name = "市场环境快照"
        verbose_name_plural = "市场环境快照"
        indexes = [models.Index(fields=["-source_as_of"], name="wheel_regime_asof_idx")]


class WheelCycle(TimestampedModel):
    family = models.ForeignKey(Family, on_delete=models.PROTECT, related_name="wheel_cycles")
    account = models.ForeignKey(InvestmentAccount, on_delete=models.PROTECT, related_name="wheel_cycles")
    underlying = models.ForeignKey(Security, on_delete=models.PROTECT, related_name="wheel_cycles")
    status = models.CharField("状态", max_length=20, choices=CycleStatus.choices, default=CycleStatus.OPEN)
    opened_on = models.DateField("开始日期")
    closed_on = models.DateField("结束日期", null=True, blank=True)
    assigned_cost_basis = models.DecimalField("指派/买入成本", max_digits=20, decimal_places=6, null=True, blank=True)
    assigned_share_quantity = models.DecimalField("本周期持有正股数量", max_digits=24, decimal_places=6, default=0)
    notes = models.TextField("备注", blank=True)

    class Meta:
        verbose_name = "车轮周期"
        verbose_name_plural = "车轮周期"
        constraints = [
            models.UniqueConstraint(fields=["account", "underlying"], condition=Q(status__in=[CycleStatus.OPEN, CycleStatus.PAUSED]), name="unique_active_wheel_cycle"),
            models.CheckConstraint(condition=Q(assigned_share_quantity__gte=0), name="wheel_cycle_shares_nonnegative"),
        ]

    def clean(self):
        errors = {}
        if self.account_id and self.family_id and self.account.family_id != self.family_id:
            errors["account"] = "账户与周期不属于同一家庭。"
        if self.closed_on and self.closed_on < self.opened_on:
            errors["closed_on"] = "结束日期不得早于开始日期。"
        if self.status == CycleStatus.CLOSED and not self.closed_on:
            errors["closed_on"] = "结束周期必须填写结束日期。"
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class WheelLeg(TimestampedModel):
    cycle = models.ForeignKey(WheelCycle, on_delete=models.PROTECT, related_name="legs")
    parent_leg = models.ForeignKey("self", on_delete=models.PROTECT, related_name="rolled_legs", null=True, blank=True)
    sequence = models.PositiveIntegerField("顺序")
    strategy = models.CharField("策略", max_length=20, choices=Strategy.choices)
    status = models.CharField("状态", max_length=20, choices=LegStatus.choices, default=LegStatus.PLANNED)
    option_quote = models.ForeignKey(WheelOptionQuoteSnapshot, on_delete=models.PROTECT, related_name="wheel_legs", null=True, blank=True)
    option_contract = models.ForeignKey(
        OptionContract,
        on_delete=models.PROTECT,
        related_name="wheel_legs",
        null=True,
        blank=True,
    )
    expiration = models.DateField("到期日", null=True, blank=True)
    strike = models.DecimalField("行权价", max_digits=20, decimal_places=6, null=True, blank=True)
    contract_count = models.PositiveIntegerField("合约数", default=1)
    open_contract_count = models.PositiveIntegerField("未平合约数", default=1)
    premium_total = models.DecimalField("累计权利金", max_digits=24, decimal_places=4, default=0)
    opened_at = models.DateTimeField("开仓时间", null=True, blank=True)
    closed_at = models.DateTimeField("结束时间", null=True, blank=True)

    class Meta:
        verbose_name = "车轮分段"
        verbose_name_plural = "车轮分段"
        ordering = ["cycle", "sequence"]
        constraints = [models.UniqueConstraint(fields=["cycle", "sequence"], name="unique_wheel_leg_sequence")]

    def clean(self):
        errors = {}
        if self.parent_leg_id and self.parent_leg.cycle_id != self.cycle_id:
            errors["parent_leg"] = "滚动前后分段必须属于同一周期。"
        if self.option_quote_id and self.option_quote.underlying_id != self.cycle.underlying_id:
            errors["option_quote"] = "期权报价与周期标的不一致。"
        if self.option_contract_id and self.option_contract.underlying_id != self.cycle.underlying_id:
            errors["option_contract"] = "期权合约与周期标的不一致。"
        if self.strategy in {Strategy.SELL_PUT, Strategy.COVERED_CALL, Strategy.ROLL}:
            if not self.option_contract_id:
                errors["option_contract"] = "活动期权分段必须绑定本地期权合约。"
            if not self.expiration or not _finite_decimal(self.strike) or self.strike <= 0:
                errors["strike"] = "期权分段必须具备有效到期日和行权价。"
        if self.open_contract_count > self.contract_count:
            errors["open_contract_count"] = "未平合约数不能超过原始合约数。"
        if self.strategy == Strategy.COVERED_CALL and _finite_decimal(self.cycle.assigned_cost_basis) and _finite_decimal(self.strike) and self.strike < self.cycle.assigned_cost_basis:
            errors["strike"] = "备兑 Call 行权价不得低于指派或买入成本。"
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class WheelTransactionLink(TimestampedModel):
    leg = models.ForeignKey(WheelLeg, on_delete=models.PROTECT, related_name="transaction_links")
    transaction = models.ForeignKey(InvestmentTransaction, on_delete=models.PROTECT, related_name="wheel_links")
    role = models.CharField("关联角色", max_length=30)
    linked_quantity = models.DecimalField(
        "关联期权张数", max_digits=24, decimal_places=6,
        default=Decimal("1"),
    )

    class Meta:
        verbose_name = "车轮交易关联"
        verbose_name_plural = "车轮交易关联"
        constraints = [
            models.UniqueConstraint(fields=["leg", "transaction", "role"], name="unique_wheel_leg_txn_role"),
            models.CheckConstraint(condition=Q(linked_quantity__gt=0), name="wheel_txn_link_qty_positive"),
        ]

    def clean(self):
        if self.transaction_id and self.leg_id:
            if self.transaction.account_id != self.leg.cycle.account_id:
                raise ValidationError({"transaction": "交易账户与车轮周期账户不一致。"})
            security = self.transaction.security
            if security and security_id_for_underlying(security) != self.leg.cycle.underlying_id:
                raise ValidationError({"transaction": "交易标的与车轮周期标的不一致。"})

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


def security_id_for_underlying(security):
    option = getattr(security, "option_contract", None)
    return option.underlying_id if option else security.pk


class WheelCollateralReservation(TimestampedModel):
    CASH = "cash"
    SHARES = "shares"
    KIND_CHOICES = [(CASH, "现金"), (SHARES, "股票")]
    leg = models.ForeignKey(WheelLeg, on_delete=models.PROTECT, related_name="collateral_reservations")
    account = models.ForeignKey(InvestmentAccount, on_delete=models.PROTECT, related_name="wheel_collateral_reservations")
    kind = models.CharField("类型", max_length=10, choices=KIND_CHOICES)
    currency = models.CharField("币种", max_length=10, blank=True)
    cash_amount = models.DecimalField("现金金额", max_digits=24, decimal_places=4, null=True, blank=True)
    share_quantity = models.DecimalField("股票数量", max_digits=24, decimal_places=6, null=True, blank=True)
    reserved_at = models.DateTimeField("预留时间", default=timezone.now)
    released_at = models.DateTimeField("释放时间", null=True, blank=True)

    class Meta:
        verbose_name = "车轮担保预留"
        verbose_name_plural = "车轮担保预留"
        constraints = [
            models.UniqueConstraint(fields=["leg"], condition=Q(released_at__isnull=True), name="unique_active_leg_collateral"),
            models.CheckConstraint(condition=(Q(kind="cash", cash_amount__gt=0, share_quantity__isnull=True) | Q(kind="shares", share_quantity__gt=0, cash_amount__isnull=True)), name="wheel_collateral_kind_amount"),
        ]

    def clean(self):
        errors = {}
        if self.leg_id and self.account_id and self.leg.cycle.account_id != self.account_id:
            errors["account"] = "担保账户与车轮周期账户不一致。"
        if self.kind == self.CASH and self.currency.upper() != "USD":
            errors["currency"] = "M1 现金担保必须是 USD。"
        if self.kind == self.SHARES and self.currency:
            errors["currency"] = "股票担保不填写币种。"
        if self.released_at and self.released_at < self.reserved_at:
            errors["released_at"] = "释放时间不得早于预留时间。"
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)
