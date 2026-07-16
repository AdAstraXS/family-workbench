from decimal import Decimal, InvalidOperation
from datetime import datetime, time, timedelta
import calendar
from django.conf import settings

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone

from family_core.models import FamilyMember
from ledger.models import BankAccount


DEFAULT_LISTING_TYPE_CHOICES = [
    ("new_listing", "新上市"),
    ("ah", "AH"),
    ("us_hk", "美港"),
    ("gem", "创业板"),
    ("wvr", "同股不同权"),
    ("other", "其他"),
]

DEFAULT_MECHANISM_CHOICES = [
    ("a", "机制A"),
    ("b", "机制B"),
    ("18a", "18A"),
    ("18c", "18C"),
]


class HkIpoListingOption(models.Model):
    CATEGORY_LISTING_TYPE = "listing_type"
    CATEGORY_MECHANISM = "mechanism"
    CATEGORY_CHOICES = [
        (CATEGORY_LISTING_TYPE, "新股类型"),
        (CATEGORY_MECHANISM, "发行机制"),
    ]

    category = models.CharField("选项类别", max_length=30, choices=CATEGORY_CHOICES)
    code = models.SlugField("选项代码", max_length=30)
    name = models.CharField("显示名称", max_length=50)
    sort_order = models.PositiveSmallIntegerField("排序", default=0)
    is_active = models.BooleanField("启用", default=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)
    _choice_cache = {}

    class Meta:
        verbose_name = "新股类型与机制选项"
        verbose_name_plural = "新股类型与机制选项"
        ordering = ["category", "sort_order", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["category", "code"],
                name="unique_ipo_listing_option_code",
            )
        ]

    def __str__(self):
        return f"{self.get_category_display()} - {self.name}"

    @classmethod
    def clear_choice_cache(cls, category=None):
        if category is None:
            cls._choice_cache.clear()
            return
        cls._choice_cache.pop(category, None)

    @classmethod
    def _get_choice_map(cls, category):
        cached = cls._choice_cache.get(category)
        if cached is not None:
            return cached
        choice_map = dict(
            cls.objects.filter(category=category, is_active=True)
            .order_by("sort_order", "id")
            .values_list("code", "name")
        )
        cls._choice_cache[category] = choice_map
        return choice_map

    @classmethod
    def choices_for(cls, category, current_value=None):
        choice_map = cls._get_choice_map(category)
        choices = list(choice_map.items())
        if current_value and current_value not in choice_map:
            choices.append((current_value, cls.display_name(category, current_value)))
        return choices

    @classmethod
    def display_name(cls, category, code):
        if not code:
            return ""
        name = cls._get_choice_map(category).get(code)
        if not name:
            name = (
                cls.objects.filter(category=category, code=code)
                .values_list("name", flat=True)
                .first()
            )
        if name:
            return name
        defaults = dict(
            DEFAULT_LISTING_TYPE_CHOICES
            if category == cls.CATEGORY_LISTING_TYPE
            else DEFAULT_MECHANISM_CHOICES
        )
        return defaults.get(code, code)

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        self.clear_choice_cache(self.category)

    def delete(self, *args, **kwargs):
        category = self.category
        super().delete(*args, **kwargs)
        self.clear_choice_cache(category)


class HkIpoListing(models.Model):
    TYPE_NEW_LISTING = "new_listing"
    TYPE_AH = "ah"
    TYPE_US_HK = "us_hk"
    TYPE_GEM = "gem"
    TYPE_WVR = "wvr"
    TYPE_OTHER = "other"
    TYPE_CHOICES = DEFAULT_LISTING_TYPE_CHOICES

    MECHANISM_A = "a"
    MECHANISM_B = "b"
    MECHANISM_18A = "18a"
    MECHANISM_18C = "18c"
    MECHANISM_CHOICES = DEFAULT_MECHANISM_CHOICES

    SPONSOR_DEALER_YES = "yes"
    SPONSOR_DEALER_LIKELY = "likely"
    SPONSOR_DEALER_UNKNOWN = "unknown"
    SPONSOR_DEALER_MARKET = "market"
    SPONSOR_DEALER_CHOICES = [
        (SPONSOR_DEALER_YES, "有"),
        (SPONSOR_DEALER_LIKELY, "大概率"),
        (SPONSOR_DEALER_UNKNOWN, "不确定"),
        (SPONSOR_DEALER_MARKET, "市场化"),
    ]

    VALUATION_LOW = "low"
    VALUATION_REASONABLE = "reasonable"
    VALUATION_HIGH = "high"
    VALUATION_EXPENSIVE = "expensive"
    VALUATION_CHOICES = [
        (VALUATION_LOW, "偏低"),
        (VALUATION_REASONABLE, "合理"),
        (VALUATION_HIGH, "偏高"),
        (VALUATION_EXPENSIVE, "很贵"),
    ]

    RECOMMEND_SKIP = "skip"
    RECOMMEND_CASH_ONE_LOT = "cash_one_lot"
    RECOMMEND_MARGIN_ONE_LOT = "margin_one_lot"
    RECOMMEND_POOL_A = "pool_a"
    RECOMMEND_POOL_B = "pool_b"
    RECOMMEND_CHOICES = [
        (RECOMMEND_SKIP, "不认购"),
        (RECOMMEND_CASH_ONE_LOT, "现金一手"),
        (RECOMMEND_MARGIN_ONE_LOT, "融资一手"),
        (RECOMMEND_POOL_A, "甲组"),
        (RECOMMEND_POOL_B, "乙组"),
    ]

    STATUS_SUBSCRIBING = "subscribing"
    STATUS_WAITING_LISTING = "waiting_listing"
    STATUS_LISTING_TODAY = "listing_today"
    STATUS_LISTED = "listed"
    SUBSCRIPTION_STATUS_CHOICES = [
        (STATUS_SUBSCRIBING, "招股中"),
        (STATUS_WAITING_LISTING, "待上市"),
        (STATUS_LISTING_TODAY, "今日上市"),
        (STATUS_LISTED, "已上市"),
    ]

    stock_code = models.CharField("股票代码", max_length=20)
    stock_name = models.CharField("股票名称", max_length=100, blank=True)
    company_name = models.CharField("公司名称", max_length=200)
    subscription_status = models.CharField("招股状态", max_length=30, choices=SUBSCRIPTION_STATUS_CHOICES, default=STATUS_LISTED)
    listing_type = models.CharField("类型", max_length=30, default=TYPE_NEW_LISTING)
    mechanism = models.CharField("机制", max_length=20, default=MECHANISM_A)
    subscription_start_date = models.DateField("招股开始日", null=True, blank=True)
    subscription_end_date = models.DateField("招股截止日", null=True, blank=True)
    allotment_result_date = models.DateField("公布结果日", null=True, blank=True)
    listing_date = models.DateField("上市日期", null=True, blank=True)
    offer_price_min = models.DecimalField("招股价下限", max_digits=12, decimal_places=4, null=True, blank=True)
    offer_price_max = models.DecimalField("招股价上限", max_digits=12, decimal_places=4, null=True, blank=True)
    final_price = models.DecimalField("最终定价", max_digits=12, decimal_places=4, null=True, blank=True)
    industry = models.CharField("行业", max_length=100, blank=True)
    over_subscription_multiple = models.DecimalField(
        "超额认购倍数",
        max_digits=14,
        decimal_places=4,
        null=True,
        blank=True,
    )
    first_day_open_change_pct = models.DecimalField(
        "首日开盘涨幅",
        max_digits=12,
        decimal_places=4,
        null=True,
        blank=True,
    )
    first_day_close_change_pct = models.DecimalField(
        "首日收盘涨幅",
        max_digits=12,
        decimal_places=4,
        null=True,
        blank=True,
    )
    cumulative_change_pct = models.DecimalField(
        "上市至今累计涨幅",
        max_digits=12,
        decimal_places=4,
        null=True,
        blank=True,
    )
    market_data_fetched_at = models.DateTimeField(
        "上市行情静态数据抓取时间",
        null=True,
        blank=True,
    )
    lot_size = models.PositiveIntegerField("每手股数", null=True, blank=True)
    entry_fee = models.DecimalField("入场费", max_digits=20, decimal_places=4, null=True, blank=True)
    public_offer_lots = models.DecimalField("公配手数", max_digits=20, decimal_places=2, null=True, blank=True)
    global_offer_shares_10k = models.DecimalField("全球发售股数（万股）", max_digits=20, decimal_places=4, null=True, blank=True)
    fundraising_amount_100m = models.DecimalField("募资金额（亿港元）", max_digits=20, decimal_places=4, null=True, blank=True)
    total_market_cap_100m = models.DecimalField("发行后总市值（亿港元）", max_digits=20, decimal_places=4, null=True, blank=True)
    h_share_market_cap_100m = models.DecimalField("H股市值（亿港元）", max_digits=20, decimal_places=4, null=True, blank=True)
    hk_connect_threshold_100m = models.DecimalField(
        "港股通门槛（亿港元）",
        max_digits=20,
        decimal_places=4,
        null=True,
        blank=True,
    )
    hk_connect_required_gain_pct = models.DecimalField("港股通预期涨幅", max_digits=12, decimal_places=4, null=True, blank=True)
    sector = models.CharField("板块", max_length=100, blank=True)
    business_summary = models.TextField("主要业务", blank=True)
    sponsor = models.CharField("保荐人", max_length=200, blank=True)
    has_sponsor_dealer = models.CharField("是否有庄家", max_length=20, choices=SPONSOR_DEALER_CHOICES, blank=True)
    has_greenshoe = models.BooleanField("绿鞋", default=False)
    stabilizing_manager = models.CharField("稳价人", max_length=200, blank=True)
    has_offer_size_adjustment = models.BooleanField("发售量调整权", default=False)
    offer_size_adjustment_pct = models.DecimalField("发售量调整比例", max_digits=8, decimal_places=4, null=True, blank=True)
    has_cornerstone = models.BooleanField("是否有基石投资者", default=False)
    cornerstone_investors = models.TextField("基石投资者名单", blank=True)
    cornerstone_pct = models.DecimalField("基石占比", max_digits=8, decimal_places=4, null=True, blank=True)
    pe_ratio = models.CharField("市盈率 PE", max_length=50, blank=True)
    ps_ratio = models.DecimalField("市销率 PS", max_digits=12, decimal_places=4, null=True, blank=True)
    comparable_companies = models.TextField("同行业可比公司", blank=True)
    valuation_comment = models.CharField("估值评价", max_length=30, choices=VALUATION_CHOICES, blank=True)
    fundamentals_score = models.PositiveSmallIntegerField("基本面评分", null=True, blank=True, validators=[MinValueValidator(1), MaxValueValidator(5)])
    heat_score = models.PositiveSmallIntegerField("热度评分", null=True, blank=True, validators=[MinValueValidator(1), MaxValueValidator(5)])
    subscription_recommendation = models.CharField("认购建议", max_length=30, choices=RECOMMEND_CHOICES, blank=True)
    decision_reason = models.TextField("决策理由", blank=True)
    prospectus = models.FileField("招股书", upload_to="ipo/prospectus/", null=True, blank=True)
    remark = models.TextField("备注", blank=True)
    extra_data = models.JSONField("扩展字段", default=dict, blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        verbose_name = "港股新股"
        verbose_name_plural = "港股新股"
        ordering = ["-subscription_end_date", "-listing_date", "stock_code"]
        indexes = [
            models.Index(fields=["stock_code"]),
            models.Index(fields=["stock_name"]),
            models.Index(fields=["subscription_status"]),
            models.Index(fields=["subscription_end_date"]),
            models.Index(fields=["listing_date"]),
            models.Index(fields=["listing_type"]),
            models.Index(fields=["mechanism"]),
        ]

    def __str__(self):
        return f"{self.stock_code} {self.company_name}"

    def get_listing_type_display(self):
        return HkIpoListingOption.display_name(
            HkIpoListingOption.CATEGORY_LISTING_TYPE,
            self.listing_type,
        )

    def get_mechanism_display(self):
        return HkIpoListingOption.display_name(
            HkIpoListingOption.CATEGORY_MECHANISM,
            self.mechanism,
        )

    @staticmethod
    def _add_calendar_months(value, months):
        month_index = value.month - 1 + months
        year = value.year + month_index // 12
        month = month_index % 12 + 1
        day = min(value.day, calendar.monthrange(year, month)[1])
        return value.replace(year=year, month=month, day=day)

    @staticmethod
    def _format_percentage(value):
        return f"{value.quantize(Decimal('0.01'))}%"

    def calculate_hk_connect_percentage(self):
        market_cap = self._to_decimal(self.h_share_market_cap_100m)
        threshold = self._to_decimal(self.hk_connect_threshold_100m)

        if self.listing_type == self.TYPE_WVR:
            if market_cap and market_cap < Decimal("200"):
                return (Decimal("200") - market_cap) / market_cap * Decimal("100")
            return None

        if self.listing_type in {self.TYPE_AH, self.TYPE_GEM}:
            return None
        if not market_cap or not threshold:
            return None
        if market_cap >= threshold * Decimal("1.2"):
            return None
        if market_cap >= threshold:
            return (market_cap / threshold - Decimal("1")) * Decimal("100")
        return (threshold - market_cap) / market_cap * Decimal("100")

    @property
    def hk_connect_expectation(self):
        market_cap = self._to_decimal(self.h_share_market_cap_100m)
        threshold = self._to_decimal(self.hk_connect_threshold_100m)

        if self.listing_type == self.TYPE_GEM:
            return "不入通"

        if self.listing_type == self.TYPE_AH:
            if not self.listing_date:
                return "待录入上市日期"
            entry_date = (
                self.listing_date + timedelta(days=28)
                if self.has_greenshoe
                else self.listing_date
            )
            return f"{entry_date:%Y-%m-%d} 入通"

        if self.listing_type == self.TYPE_WVR:
            if not market_cap:
                return "待录入H股市值"
            if market_cap < Decimal("200"):
                percentage = (Decimal("200") - market_cap) / market_cap * Decimal("100")
                return f"入通涨幅 {self._format_percentage(percentage)}"
            if not self.listing_date:
                return "待录入上市日期"
            entry_date = self._add_calendar_months(self.listing_date, 6) + timedelta(weeks=4)
            return f"{entry_date:%Y-%m-%d} 入通"

        if not market_cap:
            return "待录入H股市值"
        if not threshold:
            return "门槛数据暂不可用"
        if market_cap >= threshold * Decimal("1.2"):
            return "入通"
        if market_cap >= threshold:
            percentage = (market_cap / threshold - Decimal("1")) * Decimal("100")
            return f"入通（{self._format_percentage(percentage)}）"
        percentage = (threshold - market_cap) / market_cap * Decimal("100")
        return f"入通涨幅 {self._format_percentage(percentage)}"

    @staticmethod
    def _to_decimal(value):
        if value in (None, ""):
            return None
        try:
            return Decimal(value)
        except (InvalidOperation, TypeError, ValueError):
            return None

    def get_public_offer_ratio(self):
        if self.mechanism == self.MECHANISM_A:
            return Decimal("0.35")
        if self.mechanism == self.MECHANISM_B:
            return Decimal("0.10")
        return Decimal("0.20")

    def calculate_fields(self):
        if self.offer_price_max and not self.final_price:
            self.final_price = self.offer_price_max

        price = self._to_decimal(self.final_price)
        lot_size = self._to_decimal(self.lot_size)
        global_offer_shares_10k = self._to_decimal(self.global_offer_shares_10k)
        h_share_market_cap_100m = self._to_decimal(self.h_share_market_cap_100m)

        if price and lot_size:
            self.entry_fee = price * lot_size

        if price and global_offer_shares_10k:
            self.fundraising_amount_100m = price * global_offer_shares_10k / Decimal("10000")

        if global_offer_shares_10k and lot_size:
            self.public_offer_lots = global_offer_shares_10k * Decimal("10000") / lot_size * self.get_public_offer_ratio()

        self.hk_connect_required_gain_pct = self.calculate_hk_connect_percentage()

    def calculate_subscription_status(self, now=None):
        now = timezone.localtime(now or timezone.now())
        today = now.date()

        if self.subscription_end_date and self.subscription_end_date < datetime(2026, 3, 1).date():
            return self.STATUS_LISTED

        if self.listing_date:
            if today == self.listing_date:
                return self.STATUS_LISTING_TODAY
            if today > self.listing_date:
                return self.STATUS_LISTED

        if self.subscription_start_date and self.subscription_end_date:
            start_at = timezone.make_aware(datetime.combine(self.subscription_start_date, time.min))
            end_at = timezone.make_aware(datetime.combine(self.subscription_end_date, time(hour=10)))
            if start_at <= now < end_at:
                return self.STATUS_SUBSCRIBING
            if now >= end_at and (not self.listing_date or now.date() < self.listing_date):
                return self.STATUS_WAITING_LISTING

        return self.subscription_status or self.STATUS_LISTED

    @property
    def current_subscription_status(self):
        return self.calculate_subscription_status()

    @property
    def collision_count(self):
        cached = getattr(self, "_collision_count_cache", None)
        if cached is not None:
            return cached
        if not self.subscription_end_date:
            return 0
        query = HkIpoListing.objects.filter(subscription_end_date=self.subscription_end_date)
        if self.pk:
            query = query.exclude(pk=self.pk)
        return query.count() + 1

    @property
    def collision_label(self):
        count = self.collision_count
        if count <= 1:
            return "不撞车"
        return f"{count}股撞车"

    def save(self, *args, **kwargs):
        self.calculate_fields()
        self.subscription_status = self.calculate_subscription_status()
        super().save(*args, **kwargs)


class HkIpoSubscriptionTrade(models.Model):
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    TRANCHE_ONE_LOT = "one_lot"
    TRANCHE_MID_A = "mid_a"
    TRANCHE_TAIL_A = "tail_a"
    TRANCHE_HEAD_B = "head_b"
    TRANCHE_B2 = "b2"
    TRANCHE_B3 = "b3"
    TRANCHE_LARGE_B = "large_b"
    TRANCHE_CHOICES = [
        (TRANCHE_ONE_LOT, "一手"),
        (TRANCHE_MID_A, "中甲"),
        (TRANCHE_TAIL_A, "甲尾"),
        (TRANCHE_HEAD_B, "乙头"),
        (TRANCHE_B2, "乙2"),
        (TRANCHE_B3, "乙3"),
        (TRANCHE_LARGE_B, "大乙"),
    ]

    METHOD_CASH = "cash"
    METHOD_MARGIN = "margin"
    METHOD_CHOICES = [
        (METHOD_CASH, "现金"),
        (METHOD_MARGIN, "融资"),
    ]

    STATUS_APPLYING = "applying"
    STATUS_HOLDING = "holding"
    STATUS_CLOSED = "closed"
    STATUS_UNALLOTTED = "unallotted"
    TERMINAL_STATUSES = (STATUS_CLOSED, STATUS_UNALLOTTED)
    STATUS_CHOICES = [
        (STATUS_APPLYING, "申购中"),
        (STATUS_HOLDING, "尚持有"),
        (STATUS_CLOSED, "清仓"),
        (STATUS_UNALLOTTED, "未中签"),
    ]

    listing = models.ForeignKey(HkIpoListing, verbose_name="对应新股", on_delete=models.CASCADE, related_name="subscription_trades")
    member = models.ForeignKey(FamilyMember, verbose_name="家庭成员", on_delete=models.CASCADE, related_name="ipo_subscription_trades")
    account = models.ForeignKey(BankAccount, verbose_name="申购账户", on_delete=models.SET_NULL, null=True, blank=True, related_name="ipo_subscription_trades")
    application_date = models.DateField("申购日期", default=timezone.localdate)
    tranche = models.CharField("申购档位", max_length=20, choices=TRANCHE_CHOICES, default=TRANCHE_ONE_LOT)
    applied_lots = models.PositiveIntegerField("申购手数", default=1)
    applied_shares = models.PositiveIntegerField("申购股数", default=0)
    application_amount = models.DecimalField("申购金额", max_digits=24, decimal_places=4, default=0)
    application_method = models.CharField("申购方式", max_length=20, choices=METHOD_CHOICES, default=METHOD_MARGIN)
    financing_interest = models.DecimalField("融资利息", max_digits=20, decimal_places=4, default=0)
    subscription_fee = models.DecimalField("手续费", max_digits=20, decimal_places=4, default=100)
    trade_status = models.CharField("新股状态", max_length=20, choices=STATUS_CHOICES, default=STATUS_APPLYING)
    allotted_lots = models.PositiveIntegerField("中签手数", null=True, blank=True)
    allotted_value = models.DecimalField("中签货值", max_digits=24, decimal_places=4, default=0)
    allotment_fee = models.DecimalField("中签费用", max_digits=20, decimal_places=4, default=0)
    sell_price = models.DecimalField("卖出金额", max_digits=20, decimal_places=4, default=0)
    sell_date = models.DateField("卖出日期", null=True, blank=True)
    sold_lots = models.PositiveIntegerField("卖出手数", default=0)
    trading_fee = models.DecimalField("交易费用", max_digits=20, decimal_places=4, default=0)
    realized_profit = models.DecimalField("盈利金额", max_digits=24, decimal_places=4, default=0)
    remark = models.TextField("备注", blank=True)
    extra_data = models.JSONField("扩展字段", default=dict, blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        verbose_name = "新股申购和交易"
        verbose_name_plural = "新股申购和交易"
        ordering = ["trade_status", "-application_date", "listing__stock_code", "account__account_name"]
        indexes = [
            models.Index(fields=["listing", "trade_status"]),
            models.Index(fields=["member", "trade_status"]),
            models.Index(fields=["account", "trade_status"]),
            models.Index(fields=["application_date"]),
            models.Index(fields=["sell_date"]),
        ]

    def __str__(self):
        return f"{self.listing} {self.member} {self.get_tranche_display()}"

    @property
    def total_fees(self):
        return sum(
            (
                self.subscription_fee or Decimal("0"),
                self.allotment_fee or Decimal("0"),
                self.financing_interest or Decimal("0"),
                self.trading_fee or Decimal("0"),
            ),
            Decimal("0"),
        )

    @property
    def upfront_fees(self):
        return sum(
            (
                self.subscription_fee or Decimal("0"),
                self.allotment_fee or Decimal("0"),
                self.financing_interest or Decimal("0"),
            ),
            Decimal("0"),
        )

    @property
    def unallotted_fees(self):
        return (self.subscription_fee or Decimal("0")) + (
            self.financing_interest or Decimal("0")
        )

    def upfront_fees_for_lots(self, lots):
        allotted_lots = self.allotted_lots or 0
        if not allotted_lots:
            return Decimal("0")
        return self.upfront_fees * Decimal(lots or 0) / Decimal(allotted_lots)

    @property
    def remaining_upfront_fees(self):
        remaining_lots = max((self.allotted_lots or 0) - (self.sold_lots or 0), 0)
        return self.upfront_fees_for_lots(remaining_lots)

    @property
    def break_even_price(self):
        remaining_lots = max((self.allotted_lots or 0) - (self.sold_lots or 0), 0)
        remaining_shares = remaining_lots * (self.listing.lot_size or 0)
        if not remaining_shares:
            return None
        return (self.listing.final_price or Decimal("0")) + (
            self.remaining_upfront_fees / Decimal(remaining_shares)
        )

    @property
    def holding_value(self):
        allotted_lots = self.allotted_lots or 0
        entry_fee = self.listing.entry_fee or (
            (self.listing.final_price or Decimal("0")) * (self.listing.lot_size or 0)
        )
        return Decimal(allotted_lots) * entry_fee

    def calculate_fields(self):
        lot_size = self.listing.lot_size or 0
        final_price = self.listing.final_price or Decimal("0")
        entry_fee = self.listing.entry_fee or final_price * lot_size
        applied_lots = self.applied_lots or 0
        allotted_lots = self.allotted_lots
        sold_lots = self.sold_lots or 0

        self.applied_shares = applied_lots * lot_size
        self.application_amount = Decimal(self.applied_shares) * final_price
        if allotted_lots is None:
            self.trade_status = self.STATUS_APPLYING
            self.allotted_value = Decimal("0")
            self.allotment_fee = Decimal("0")
        else:
            self.allotted_value = Decimal(allotted_lots) * entry_fee
            self.allotment_fee = self.allotted_value * Decimal("0.01")
            if allotted_lots == 0:
                self.trade_status = self.STATUS_UNALLOTTED
                if not self.sell_date and self.listing.allotment_result_date:
                    self.sell_date = self.listing.allotment_result_date
            else:
                self.trade_status = (
                    self.STATUS_CLOSED
                    if sold_lots >= allotted_lots
                    else self.STATUS_HOLDING
                )

        if allotted_lots == 0:
            self.realized_profit = -self.unallotted_fees
        else:
            self.realized_profit = (
                ((self.sell_price or Decimal("0")) - final_price)
                * Decimal(sold_lots)
                * Decimal(lot_size)
                - self.upfront_fees_for_lots(sold_lots)
                - (self.trading_fee or Decimal("0"))
            )

    def save(self, *args, **kwargs):
        old_sell_date = self.sell_date
        self.calculate_fields()
        update_fields = kwargs.get("update_fields")
        if update_fields is not None and self.sell_date != old_sell_date:
            kwargs["update_fields"] = set(update_fields) | {"sell_date"}
        super().save(*args, **kwargs)
