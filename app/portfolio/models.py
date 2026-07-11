import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.urls import reverse

from family_core.models import (
    AccountType,
    AssetCategory,
    Family,
    FamilyMember,
    TimestampedModel,
)


class VisibilityChoices(models.TextChoices):
    PRIVATE = "private", "仅本人"
    FAMILY = "family", "家庭可见"
    ADMIN_ONLY = "admin_only", "仅管理员"


class TradeTypeChoices(models.TextChoices):
    BUY = "buy", "买入"
    IPO = "ipo", "打新"
    SELL = "sell", "卖出"
    DIVIDEND = "dividend", "分红"
    INTEREST = "interest", "利息"
    OTHER_FEE_ADJUSTMENT = "other_fee_adjustment", "其他费用调整"
    OTHER = "other", "其他"


class TradeStatusChoices(models.TextChoices):
    PLANNED = "planned", "计划中"
    SUBMITTED = "submitted", "已提交"
    PARTIAL = "partial", "部分成交"
    COMPLETED = "completed", "已成交"
    CANCELLED = "cancelled", "已取消"


class TransactionSourceChoices(models.TextChoices):
    MANUAL = "manual", "手工录入"
    IMPORT = "import", "文件导入"
    FUTU = "futu", "Futu 同步"
    IPO = "ipo", "港股打新"


class InvestmentOption(TimestampedModel):
    CATEGORY_TRANSACTION_TYPE = "transaction_type"
    CATEGORY_INFORMATION_SOURCE = "information_source"
    CATEGORY_STRATEGY_TYPE = "strategy_type"
    CATEGORY_EMOTION = "emotion"
    CATEGORY_CHOICES = [
        (CATEGORY_TRANSACTION_TYPE, "交易类型"),
        (CATEGORY_INFORMATION_SOURCE, "信息来源"),
        (CATEGORY_STRATEGY_TYPE, "交易策略"),
        (CATEGORY_EMOTION, "交易情绪"),
    ]

    category = models.CharField("选项类别", max_length=30, choices=CATEGORY_CHOICES)
    code = models.SlugField("选项代码", max_length=50)
    name = models.CharField("显示名称", max_length=200)
    sort_order = models.PositiveSmallIntegerField("排序", default=0)
    is_active = models.BooleanField("启用", default=True)

    class Meta:
        verbose_name = "投资交易选项"
        verbose_name_plural = "投资交易选项"
        ordering = ["category", "sort_order", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["category", "code"],
                name="unique_investment_option_code",
            )
        ]

    def __str__(self):
        return self.name


class CashMovementTypeChoices(models.TextChoices):
    DEPOSIT = "deposit", "入金"
    WITHDRAWAL = "withdrawal", "出金"
    BUY = "buy", "买入"
    SELL = "sell", "卖出"
    DIVIDEND = "dividend", "股息"
    INTEREST = "interest", "利息"
    FEE = "fee", "费用"
    TAX = "tax", "税费"
    EXCHANGE = "exchange", "换汇"
    TRANSFER = "transfer", "转账"
    ADJUSTMENT = "adjustment", "余额调整"


class InvestmentAccount(TimestampedModel):
    bank_account = models.OneToOneField(
        "ledger.BankAccount",
        verbose_name="关联账户",
        on_delete=models.PROTECT,
        related_name="investment_profile",
        null=False,
        blank=False,
    )
    extra_data = models.JSONField("扩展字段", default=dict, blank=True)

    class Meta:
        verbose_name = "投资账户"
        verbose_name_plural = "投资账户"

    @property
    def family(self):
        return self.bank_account.family

    @property
    def family_id(self):
        return self.bank_account.family_id

    @property
    def member(self):
        return self.bank_account.member

    @property
    def member_id(self):
        return self.bank_account.member_id

    @property
    def account_region(self):
        return self.bank_account.account_region

    @property
    def account_name(self):
        return self.bank_account.account_name

    @property
    def account_no_masked(self):
        return self.bank_account.account_no_masked

    @property
    def visibility(self):
        return VisibilityChoices.FAMILY

    @property
    def is_active(self):
        return self.bank_account.is_active

    @property
    def remark(self):
        return self.bank_account.remark

    def __str__(self):
        return f"{self.member} - {self.account_name}"

    def get_absolute_url(self):
        return reverse("portfolio:account_detail", args=[self.pk])


class Security(TimestampedModel):
    asset_category = models.ForeignKey(
        AssetCategory,
        verbose_name="一级资产类别",
        on_delete=models.SET_NULL,
        related_name="securities",
        null=True,
        blank=True,
    )
    symbol = models.CharField("代码", max_length=30)
    name = models.CharField("名称", max_length=200)
    market = models.CharField("市场", max_length=20)
    exchange = models.CharField("交易所", max_length=30, blank=True)
    asset_type = models.CharField("资产类型", max_length=30, default="stock")
    currency = models.CharField("交易币种", max_length=10, default="CNY")
    industry = models.CharField("行业", max_length=100, blank=True)
    lot_size = models.PositiveIntegerField("每手股数", default=0)
    listing_date = models.DateField("上市日期", null=True, blank=True)
    is_delisted = models.BooleanField("是否退市", default=False)
    data_source = models.CharField("数据来源", max_length=30, default="manual")
    source_updated_at = models.DateTimeField("来源更新时间", null=True, blank=True)
    is_active = models.BooleanField("是否有效", default=True)
    extra_data = models.JSONField("扩展字段", default=dict, blank=True)

    class Meta:
        verbose_name = "证券标的"
        verbose_name_plural = "证券标的"
        indexes = [
            models.Index(fields=["symbol", "market"]),
            models.Index(fields=["asset_type"]),
            models.Index(fields=["is_active"]),
        ]
        constraints = [
            models.UniqueConstraint(fields=["symbol", "market"], name="unique_security_symbol_market")
        ]

    def __str__(self):
        return f"{self.symbol} {self.name}"

    @property
    def futu_url(self):
        suffix = self.exchange or self.market
        prefix = "/hk" if self.market == "HK" else ""
        return f"https://www.futunn.com{prefix}/stock/{self.symbol}-{suffix}"


class WatchlistItem(TimestampedModel):
    family = models.ForeignKey(
        Family,
        verbose_name="所属家庭",
        on_delete=models.CASCADE,
        related_name="watchlist_items",
    )
    member = models.ForeignKey(
        FamilyMember,
        verbose_name="添加成员",
        on_delete=models.SET_NULL,
        related_name="watchlist_items",
        null=True,
        blank=True,
    )
    security = models.ForeignKey(
        Security,
        verbose_name="证券标的",
        on_delete=models.CASCADE,
        related_name="watchlist_items",
    )
    is_active = models.BooleanField("是否关注", default=True)
    remark = models.TextField("关注备注", blank=True)

    class Meta:
        verbose_name = "自选股"
        verbose_name_plural = "自选股"
        ordering = ["security__market", "security__symbol"]
        constraints = [
            models.UniqueConstraint(
                fields=["family", "security"],
                name="unique_family_watchlist_security",
            )
        ]
        indexes = [
            models.Index(fields=["family", "is_active"]),
        ]

    def __str__(self):
        return f"{self.family} - {self.security}"


class SecurityMarketSnapshot(models.Model):
    security = models.OneToOneField(
        Security,
        verbose_name="证券标的",
        on_delete=models.CASCADE,
        related_name="market_snapshot",
    )
    quote_time = models.CharField("行情时间", max_length=50, blank=True)
    last_price = models.DecimalField("最新价", max_digits=20, decimal_places=6, null=True, blank=True)
    change_rate = models.DecimalField("当日涨跌幅", max_digits=20, decimal_places=6, null=True, blank=True)
    total_market_value = models.DecimalField("总市值", max_digits=24, decimal_places=4, null=True, blank=True)
    pe_ratio = models.DecimalField("市盈率", max_digits=20, decimal_places=6, null=True, blank=True)
    pe_ttm_ratio = models.DecimalField("市盈率 TTM", max_digits=20, decimal_places=6, null=True, blank=True)
    pb_ratio = models.DecimalField("市净率", max_digits=20, decimal_places=6, null=True, blank=True)
    ps_ratio = models.DecimalField("市销率", max_digits=20, decimal_places=6, null=True, blank=True)
    dividend_yield_ttm = models.DecimalField("股息率 TTM", max_digits=20, decimal_places=6, null=True, blank=True)
    turnover_rate = models.DecimalField("换手率", max_digits=20, decimal_places=6, null=True, blank=True)
    high_52_week = models.DecimalField("52 周最高", max_digits=20, decimal_places=6, null=True, blank=True)
    low_52_week = models.DecimalField("52 周最低", max_digits=20, decimal_places=6, null=True, blank=True)
    issued_shares = models.BigIntegerField("总股本", null=True, blank=True)
    outstanding_shares = models.BigIntegerField("流通股本", null=True, blank=True)
    raw_data = models.JSONField("原始数据", default=dict, blank=True)
    fetched_at = models.DateTimeField("获取时间", auto_now=True)

    class Meta:
        verbose_name = "证券行情快照"
        verbose_name_plural = "证券行情快照"

    def __str__(self):
        return f"{self.security} {self.quote_time}"


class InvestmentPosition(TimestampedModel):
    account = models.ForeignKey(InvestmentAccount, verbose_name="投资账户", on_delete=models.CASCADE, related_name="positions")
    security = models.ForeignKey(Security, verbose_name="证券标的", on_delete=models.CASCADE, related_name="positions")
    quantity = models.DecimalField("持仓数量", max_digits=24, decimal_places=6, default=0)
    avg_cost = models.DecimalField("平均成本", max_digits=20, decimal_places=6, default=0)
    diluted_cost = models.DecimalField("摊薄成本", max_digits=20, decimal_places=6, default=0)
    current_price = models.DecimalField("当前价格", max_digits=20, decimal_places=6, default=0)
    market_value = models.DecimalField("当前市值", max_digits=20, decimal_places=4, default=0)
    unrealized_pnl = models.DecimalField("浮动盈亏", max_digits=20, decimal_places=4, default=0)
    realized_pnl = models.DecimalField("累计已实现盈亏", max_digits=20, decimal_places=4, default=0)
    pnl_ratio = models.DecimalField("盈亏比例", max_digits=12, decimal_places=6, default=0)
    position_date = models.DateField("持仓日期")
    remark = models.TextField("备注", blank=True)
    extra_data = models.JSONField("扩展字段", default=dict, blank=True)

    class Meta:
        verbose_name = "投资持仓"
        verbose_name_plural = "投资持仓"
        indexes = [
            models.Index(fields=["account", "security", "position_date"]),
            models.Index(fields=["position_date"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["account", "security"],
                name="unique_current_investment_position",
            )
        ]

    def __str__(self):
        return f"{self.account} - {self.security} - {self.position_date}"


class InvestmentTransaction(TimestampedModel):
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    account = models.ForeignKey(InvestmentAccount, verbose_name="投资账户", on_delete=models.CASCADE, related_name="transactions")
    security = models.ForeignKey(Security, verbose_name="证券标的", on_delete=models.CASCADE, related_name="transactions", null=True, blank=True)
    asset_category = models.ForeignKey(
        AssetCategory,
        verbose_name="资产类别",
        on_delete=models.SET_NULL,
        related_name="investment_transactions",
        null=True,
        blank=True,
    )
    trade_date = models.DateField("交易日期")
    trade_type = models.CharField("交易类型", max_length=30, choices=TradeTypeChoices.choices)
    trade_type_option = models.ForeignKey(
        InvestmentOption,
        verbose_name="交易类型选项",
        on_delete=models.PROTECT,
        related_name="typed_transactions",
        null=True,
        blank=True,
        limit_choices_to={"category": InvestmentOption.CATEGORY_TRANSACTION_TYPE},
    )
    transaction_no = models.CharField(
        "交易编号",
        max_length=40,
        unique=True,
        null=True,
        blank=True,
        editable=False,
    )
    status = models.CharField("交易状态", max_length=20, choices=TradeStatusChoices.choices, default=TradeStatusChoices.COMPLETED)
    quantity = models.DecimalField("数量", max_digits=24, decimal_places=6, default=0)
    price = models.DecimalField("价格", max_digits=20, decimal_places=6, default=0)
    amount = models.DecimalField("成交金额", max_digits=20, decimal_places=4, default=0)
    fee = models.DecimalField("手续费", max_digits=20, decimal_places=4, default=0)
    tax = models.DecimalField("税费", max_digits=20, decimal_places=4, default=0)
    cash_change = models.DecimalField("现金变动", max_digits=20, decimal_places=4, default=0)
    currency = models.CharField("币种", max_length=10, blank=True, default="")
    sell_cost = models.DecimalField("卖出成本", max_digits=20, decimal_places=4, default=0)
    realized_pnl = models.DecimalField("已实现盈亏", max_digits=20, decimal_places=4, default=0)
    realized_return_ratio = models.DecimalField("已实现收益率", max_digits=12, decimal_places=6, default=0)
    source = models.CharField("数据来源", max_length=20, choices=TransactionSourceChoices.choices, default=TransactionSourceChoices.MANUAL)
    external_id = models.CharField("外部流水号", max_length=200, blank=True)
    ipo_subscription_trade = models.ForeignKey(
        "ipo.HkIpoSubscriptionTrade",
        verbose_name="港股打新申购",
        on_delete=models.SET_NULL,
        related_name="investment_transactions",
        null=True,
        blank=True,
    )
    trade_logic = models.TextField("交易逻辑", blank=True)
    information_source = models.CharField("信息来源", max_length=200, blank=True)
    information_source_option = models.ForeignKey(
        InvestmentOption,
        verbose_name="信息来源选项",
        on_delete=models.SET_NULL,
        related_name="source_transactions",
        null=True,
        blank=True,
        limit_choices_to={"category": InvestmentOption.CATEGORY_INFORMATION_SOURCE},
    )
    strategy_type = models.CharField("交易策略", max_length=50, blank=True)
    strategy_option = models.ForeignKey(
        InvestmentOption,
        verbose_name="交易策略选项",
        on_delete=models.SET_NULL,
        related_name="strategy_transactions",
        null=True,
        blank=True,
        limit_choices_to={"category": InvestmentOption.CATEGORY_STRATEGY_TYPE},
    )
    strategy_other = models.CharField("其他交易策略", max_length=100, blank=True)
    emotion = models.CharField("交易情绪", max_length=30, blank=True)
    emotion_option = models.ForeignKey(
        InvestmentOption,
        verbose_name="交易情绪选项",
        on_delete=models.SET_NULL,
        related_name="emotion_transactions",
        null=True,
        blank=True,
        limit_choices_to={"category": InvestmentOption.CATEGORY_EMOTION},
    )
    exit_condition = models.TextField("退出条件", blank=True)
    remark = models.TextField("备注", blank=True)
    extra_data = models.JSONField("扩展字段", default=dict, blank=True)

    class Meta:
        verbose_name = "投资交易记录"
        verbose_name_plural = "投资交易记录"
        indexes = [
            models.Index(fields=["account", "trade_date"]),
            models.Index(fields=["security", "trade_date"]),
            models.Index(fields=["trade_type"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["account", "source", "external_id"],
                condition=~Q(external_id=""),
                name="unique_portfolio_external_transaction",
            )
        ]

    def __str__(self):
        target = self.security or "现金/其他"
        return f"{self.trade_date} {self.trade_type} {target}"

    def save(self, *args, **kwargs):
        if not self.transaction_no:
            self.transaction_no = f"TXN-{uuid.uuid4().hex}"
        super().save(*args, **kwargs)


class DailyExchangeRateFetch(models.Model):
    fetch_date = models.DateField("抓取日期", unique=True)
    source_date = models.DateField("汇率日期", null=True, blank=True)
    status = models.CharField("状态", max_length=20, default="success")
    error_message = models.TextField("错误信息", blank=True)
    fetched_at = models.DateTimeField("抓取时间", auto_now=True)

    class Meta:
        verbose_name = "每日汇率抓取"
        verbose_name_plural = "每日汇率抓取"

    def __str__(self):
        return f"{self.fetch_date} {self.status}"


class InvestmentCashMovement(TimestampedModel):
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    account = models.ForeignKey(
        InvestmentAccount,
        verbose_name="投资账户",
        on_delete=models.CASCADE,
        related_name="cash_movements",
    )
    transaction = models.OneToOneField(
        InvestmentTransaction,
        verbose_name="关联交易",
        on_delete=models.CASCADE,
        related_name="cash_movement",
        null=True,
        blank=True,
    )
    counterparty_account = models.ForeignKey(
        "ledger.BankAccount",
        verbose_name="对手账户",
        on_delete=models.SET_NULL,
        related_name="investment_cash_counterparties",
        null=True,
        blank=True,
    )
    movement_date = models.DateField("发生日期")
    settlement_date = models.DateField("结算日期", null=True, blank=True)
    movement_type = models.CharField(
        "变动类型",
        max_length=30,
        choices=CashMovementTypeChoices.choices,
    )
    currency = models.CharField("币种", max_length=10)
    amount = models.DecimalField("变动金额", max_digits=20, decimal_places=4)
    source = models.CharField(
        "数据来源",
        max_length=20,
        choices=TransactionSourceChoices.choices,
        default=TransactionSourceChoices.MANUAL,
    )
    external_id = models.CharField("外部流水号", max_length=200, blank=True)
    remark = models.TextField("备注", blank=True)

    class Meta:
        verbose_name = "投资账户现金流水"
        verbose_name_plural = "投资账户现金流水"
        ordering = ["movement_date", "created_at", "pk"]
        indexes = [
            models.Index(fields=["account", "currency", "movement_date"]),
            models.Index(fields=["movement_type", "movement_date"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["account", "source", "external_id"],
                condition=~Q(external_id=""),
                name="unique_portfolio_external_cash_movement",
            )
        ]

    def __str__(self):
        return f"{self.movement_date} {self.account} {self.amount} {self.currency}"


class PortfolioSnapshot(models.Model):
    family = models.ForeignKey(Family, verbose_name="所属家庭", on_delete=models.CASCADE, related_name="portfolio_snapshots")
    member = models.ForeignKey(FamilyMember, verbose_name="所属成员", on_delete=models.CASCADE, related_name="portfolio_snapshots", null=True, blank=True)
    account = models.ForeignKey(InvestmentAccount, verbose_name="投资账户", on_delete=models.CASCADE, related_name="snapshots", null=True, blank=True)
    snapshot_date = models.DateField("快照日期")
    total_cash = models.DecimalField("现金", max_digits=20, decimal_places=4, default=0)
    total_market_value = models.DecimalField("持仓市值", max_digits=20, decimal_places=4, default=0)
    total_asset = models.DecimalField("总资产", max_digits=20, decimal_places=4, default=0)
    total_cost = models.DecimalField("总成本", max_digits=20, decimal_places=4, default=0)
    total_pnl = models.DecimalField("总盈亏", max_digits=20, decimal_places=4, default=0)
    pnl_ratio = models.DecimalField("盈亏比例", max_digits=12, decimal_places=6, default=0)
    currency = models.CharField("币种", max_length=10, default="CNY")
    extra_data = models.JSONField("扩展字段", default=dict, blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        verbose_name = "投资组合快照"
        verbose_name_plural = "投资组合快照"
        indexes = [
            models.Index(fields=["family", "member", "snapshot_date"]),
            models.Index(fields=["account", "snapshot_date"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["family", "member", "account", "snapshot_date", "currency"],
                name="unique_portfolio_snapshot_scope_date_currency",
                nulls_distinct=False,
            )
        ]

    def __str__(self):
        return f"{self.family} {self.snapshot_date} {self.total_asset}"


class PortfolioSnapshotPositionLine(models.Model):
    snapshot = models.ForeignKey(
        PortfolioSnapshot,
        verbose_name="组合快照",
        on_delete=models.CASCADE,
        related_name="position_lines",
    )
    account = models.ForeignKey(
        InvestmentAccount,
        verbose_name="投资账户",
        on_delete=models.PROTECT,
        related_name="snapshot_position_lines",
    )
    security = models.ForeignKey(
        Security,
        verbose_name="证券标的",
        on_delete=models.PROTECT,
        related_name="snapshot_position_lines",
        null=True,
        blank=True,
    )
    asset_type = models.CharField("资产类型", max_length=30)
    asset_name = models.CharField("资产名称", max_length=200)
    quantity = models.DecimalField("数量", max_digits=24, decimal_places=6, default=0)
    price = models.DecimalField("快照价格", max_digits=20, decimal_places=6, default=0)
    currency = models.CharField("原币", max_length=10)
    fx_rate = models.DecimalField("折算汇率", max_digits=20, decimal_places=8, default=1)
    market_value_original = models.DecimalField("原币市值", max_digits=20, decimal_places=4, default=0)
    market_value = models.DecimalField("本位币市值", max_digits=20, decimal_places=4, default=0)
    cost_original = models.DecimalField("原币成本", max_digits=20, decimal_places=4, default=0)
    cost = models.DecimalField("本位币成本", max_digits=20, decimal_places=4, default=0)
    unrealized_pnl = models.DecimalField("本位币浮动盈亏", max_digits=20, decimal_places=4, default=0)

    class Meta:
        verbose_name = "组合快照持仓明细"
        verbose_name_plural = "组合快照持仓明细"
        ordering = ["account_id", "asset_type", "asset_name"]
        indexes = [models.Index(fields=["snapshot", "asset_type"])]


class SecurityNews(models.Model):
    security = models.ForeignKey(Security, verbose_name="证券标的", on_delete=models.CASCADE, related_name="news")
    title = models.CharField("新闻标题", max_length=300)
    summary = models.TextField("摘要", blank=True)
    url = models.URLField("链接", max_length=1000, blank=True)
    source = models.CharField("来源", max_length=100, blank=True)
    published_at = models.DateTimeField("发布时间", null=True, blank=True)
    sentiment = models.CharField("情绪", max_length=20, blank=True)
    raw_data = models.JSONField("原始数据", default=dict, blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        verbose_name = "股票新闻缓存"
        verbose_name_plural = "股票新闻缓存"
        indexes = [
            models.Index(fields=["security", "published_at"]),
            models.Index(fields=["source"]),
        ]

    def __str__(self):
        return self.title
