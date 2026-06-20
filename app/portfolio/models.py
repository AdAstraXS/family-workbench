from django.db import models

from family_core.models import Family, FamilyMember, TimestampedModel


class VisibilityChoices(models.TextChoices):
    PRIVATE = "private", "仅本人"
    FAMILY = "family", "家庭可见"
    ADMIN_ONLY = "admin_only", "仅管理员"


class InvestmentAccount(TimestampedModel):
    family = models.ForeignKey(Family, verbose_name="所属家庭", on_delete=models.CASCADE, related_name="investment_accounts")
    member = models.ForeignKey(FamilyMember, verbose_name="所属成员", on_delete=models.CASCADE, related_name="investment_accounts")
    broker_name = models.CharField("券商名称", max_length=100)
    account_name = models.CharField("账户名称", max_length=100)
    account_no_masked = models.CharField("脱敏账号", max_length=100, blank=True)
    market_scope = models.CharField("市场范围", max_length=50, blank=True)
    currency = models.CharField("主要币种", max_length=10, default="CNY")
    cash_balance = models.DecimalField("现金余额", max_digits=20, decimal_places=4, default=0)
    visibility = models.CharField("可见范围", max_length=20, choices=VisibilityChoices.choices, default=VisibilityChoices.PRIVATE)
    is_active = models.BooleanField("是否有效", default=True)
    remark = models.TextField("备注", blank=True)
    extra_data = models.JSONField("扩展字段", default=dict, blank=True)

    class Meta:
        verbose_name = "投资账户"
        verbose_name_plural = "投资账户"
        indexes = [
            models.Index(fields=["family", "member"]),
            models.Index(fields=["broker_name"]),
            models.Index(fields=["is_active"]),
        ]

    def __str__(self):
        return f"{self.member} - {self.broker_name} - {self.account_name}"


class Security(TimestampedModel):
    symbol = models.CharField("代码", max_length=30)
    name = models.CharField("名称", max_length=200)
    market = models.CharField("市场", max_length=20)
    asset_type = models.CharField("资产类型", max_length=30, default="stock")
    currency = models.CharField("交易币种", max_length=10, default="CNY")
    industry = models.CharField("行业", max_length=100, blank=True)
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


class InvestmentPosition(TimestampedModel):
    account = models.ForeignKey(InvestmentAccount, verbose_name="投资账户", on_delete=models.CASCADE, related_name="positions")
    security = models.ForeignKey(Security, verbose_name="证券标的", on_delete=models.CASCADE, related_name="positions")
    quantity = models.DecimalField("持仓数量", max_digits=24, decimal_places=6, default=0)
    avg_cost = models.DecimalField("平均成本", max_digits=20, decimal_places=6, default=0)
    current_price = models.DecimalField("当前价格", max_digits=20, decimal_places=6, default=0)
    market_value = models.DecimalField("当前市值", max_digits=20, decimal_places=4, default=0)
    unrealized_pnl = models.DecimalField("浮动盈亏", max_digits=20, decimal_places=4, default=0)
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

    def __str__(self):
        return f"{self.account} - {self.security} - {self.position_date}"


class InvestmentTransaction(TimestampedModel):
    account = models.ForeignKey(InvestmentAccount, verbose_name="投资账户", on_delete=models.CASCADE, related_name="transactions")
    security = models.ForeignKey(Security, verbose_name="证券标的", on_delete=models.CASCADE, related_name="transactions", null=True, blank=True)
    trade_date = models.DateField("交易日期")
    trade_type = models.CharField("交易类型", max_length=30)
    quantity = models.DecimalField("数量", max_digits=24, decimal_places=6, default=0)
    price = models.DecimalField("价格", max_digits=20, decimal_places=6, default=0)
    amount = models.DecimalField("成交金额", max_digits=20, decimal_places=4, default=0)
    fee = models.DecimalField("手续费", max_digits=20, decimal_places=4, default=0)
    tax = models.DecimalField("税费", max_digits=20, decimal_places=4, default=0)
    currency = models.CharField("币种", max_length=10, default="CNY")
    realized_pnl = models.DecimalField("已实现盈亏", max_digits=20, decimal_places=4, default=0)
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

    def __str__(self):
        target = self.security or "现金/其他"
        return f"{self.trade_date} {self.trade_type} {target}"


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

    def __str__(self):
        return f"{self.family} {self.snapshot_date} {self.total_asset}"


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
