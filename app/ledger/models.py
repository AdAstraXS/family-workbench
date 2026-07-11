from django.conf import settings
from django.db import models

from family_core.models import AccountRegion, AccountType, AssetCategory, Family, FamilyMember, TimestampedModel
from portfolio.models import VisibilityChoices


class BankAccount(TimestampedModel):
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    family = models.ForeignKey(Family, verbose_name="所属家庭", on_delete=models.CASCADE, related_name="bank_accounts")
    member = models.ForeignKey(FamilyMember, verbose_name="所属成员", on_delete=models.CASCADE, related_name="bank_accounts")
    account_name = models.CharField("账户名称", max_length=100)
    account_no_masked = models.CharField("脱敏账号", max_length=100, blank=True)
    account_type_ref = models.ForeignKey(AccountType, verbose_name="账户类型", on_delete=models.SET_NULL, null=True, blank=True, related_name="accounts")
    account_region = models.ForeignKey(AccountRegion, verbose_name="账户地区", on_delete=models.SET_NULL, null=True, blank=True, related_name="accounts")
    is_active = models.BooleanField("是否有效", default=True)
    remark = models.TextField("备注", blank=True)
    extra_data = models.JSONField("扩展字段", default=dict, blank=True)

    class Meta:
        verbose_name = "账户"
        verbose_name_plural = "账户"
        indexes = [
            models.Index(fields=["family", "member"]),
            models.Index(fields=["is_active"]),
        ]

    def __str__(self):
        return f"{self.member} - {self.account_name}"


class IncomeCategory(models.Model):
    family = models.ForeignKey(Family, verbose_name="所属家庭", on_delete=models.CASCADE, related_name="income_categories")
    name = models.CharField("分类名称", max_length=100)
    parent = models.ForeignKey("self", verbose_name="父分类", on_delete=models.SET_NULL, null=True, blank=True)
    is_active = models.BooleanField("是否有效", default=True)
    extra_data = models.JSONField("扩展字段", default=dict, blank=True)

    class Meta:
        verbose_name = "收入分类"
        verbose_name_plural = "收入分类"
        constraints = [
            models.UniqueConstraint(fields=["family", "name", "parent"], name="unique_income_category")
        ]

    def __str__(self):
        return "-".join(self.path_names)

    @property
    def path_names(self):
        names = [self.name]
        category = self.parent
        visited = {self.pk}
        while category and category.pk not in visited:
            names.append(category.name)
            visited.add(category.pk)
            category = category.parent
        return list(reversed(names))

    @property
    def category_level(self):
        return len(self.path_names)


class ExpenseCategory(models.Model):
    family = models.ForeignKey(Family, verbose_name="所属家庭", on_delete=models.CASCADE, related_name="expense_categories")
    name = models.CharField("分类名称", max_length=100)
    parent = models.ForeignKey("self", verbose_name="父分类", on_delete=models.SET_NULL, null=True, blank=True)
    is_active = models.BooleanField("是否有效", default=True)
    extra_data = models.JSONField("扩展字段", default=dict, blank=True)

    class Meta:
        verbose_name = "支出分类"
        verbose_name_plural = "支出分类"
        constraints = [
            models.UniqueConstraint(fields=["family", "name", "parent"], name="unique_expense_category")
        ]

    def __str__(self):
        return "-".join(self.path_names)

    @property
    def path_names(self):
        names = [self.name]
        category = self.parent
        visited = {self.pk}
        while category and category.pk not in visited:
            names.append(category.name)
            visited.add(category.pk)
            category = category.parent
        return list(reversed(names))

    @property
    def category_level(self):
        return len(self.path_names)


class IncomeRecord(TimestampedModel):
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    family = models.ForeignKey(Family, verbose_name="所属家庭", on_delete=models.CASCADE, related_name="income_records")
    member = models.ForeignKey(FamilyMember, verbose_name="所属成员", on_delete=models.CASCADE, related_name="income_records")
    bank_account = models.ForeignKey(BankAccount, verbose_name="入账账户", on_delete=models.SET_NULL, related_name="income_records", null=True, blank=True)
    category = models.ForeignKey(IncomeCategory, verbose_name="收入分类", on_delete=models.SET_NULL, related_name="income_records", null=True, blank=True)
    income_date = models.DateField("收入日期")
    period_start = models.DateField("统计开始日期", null=True, blank=True)
    period_end = models.DateField("统计结束日期", null=True, blank=True)
    amount = models.DecimalField("金额", max_digits=20, decimal_places=4)
    currency = models.CharField("币种", max_length=10, default="CNY")
    source_name = models.CharField("来源", max_length=100, blank=True)
    is_recurring = models.BooleanField("是否周期收入", default=False)
    visibility = models.CharField("可见范围", max_length=20, choices=VisibilityChoices.choices, default=VisibilityChoices.PRIVATE)
    remark = models.TextField("备注", blank=True)
    extra_data = models.JSONField("扩展字段", default=dict, blank=True)

    class Meta:
        verbose_name = "收入记录"
        verbose_name_plural = "收入记录"
        indexes = [
            models.Index(fields=["family", "member", "income_date"]),
            models.Index(fields=["family", "member", "period_start", "period_end"]),
            models.Index(fields=["category"]),
            models.Index(fields=["bank_account"]),
        ]

    def __str__(self):
        return f"{self.income_date} {self.member} {self.amount}"


class ExpenseImportBatch(TimestampedModel):
    STATUS_COMPLETED = "completed"
    STATUS_CHOICES = [
        (STATUS_COMPLETED, "已完成"),
    ]

    family = models.ForeignKey(Family, verbose_name="所属家庭", on_delete=models.CASCADE, related_name="expense_import_batches")
    imported_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="导入用户",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="expense_import_batches",
    )
    source_filename = models.CharField("源文件名", max_length=255)
    source_sha256 = models.CharField("文件校验值", max_length=64)
    worksheet_name = models.CharField("工作表", max_length=255, blank=True)
    row_count = models.PositiveIntegerField("源数据行数", default=0)
    imported_count = models.PositiveIntegerField("新增行数", default=0)
    skipped_count = models.PositiveIntegerField("跳过重复行数", default=0)
    total_amount = models.DecimalField("源文件净支出", max_digits=20, decimal_places=4, default=0)
    status = models.CharField("状态", max_length=20, choices=STATUS_CHOICES, default=STATUS_COMPLETED)
    extra_data = models.JSONField("扩展字段", default=dict, blank=True)

    class Meta:
        verbose_name = "支出导入批次"
        verbose_name_plural = "支出导入批次"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["family", "source_sha256"], name="unique_expense_import_file_per_family"),
        ]
        indexes = [
            models.Index(fields=["family", "created_at"]),
        ]

    def __str__(self):
        return f"{self.source_filename}（{self.imported_count} 笔）"


class ExpenseRecord(TimestampedModel):
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    family = models.ForeignKey(Family, verbose_name="所属家庭", on_delete=models.CASCADE, related_name="expense_records")
    member = models.ForeignKey(FamilyMember, verbose_name="所属成员", on_delete=models.CASCADE, related_name="expense_records")
    bank_account = models.ForeignKey(BankAccount, verbose_name="支出账户", on_delete=models.SET_NULL, related_name="expense_records", null=True, blank=True)
    category = models.ForeignKey(ExpenseCategory, verbose_name="支出分类", on_delete=models.SET_NULL, related_name="expense_records", null=True, blank=True)
    expense_date = models.DateField("支出日期")
    occurred_at = models.DateTimeField("支出时间", null=True, blank=True)
    period_start = models.DateField("统计开始日期", null=True, blank=True)
    period_end = models.DateField("统计结束日期", null=True, blank=True)
    amount = models.DecimalField("金额", max_digits=20, decimal_places=4)
    currency = models.CharField("币种", max_length=10, default="CNY")
    merchant = models.CharField("商户或对象", max_length=100, blank=True)
    payment_method = models.CharField("支付方式", max_length=50, blank=True)
    visibility = models.CharField("可见范围", max_length=20, choices=VisibilityChoices.choices, default=VisibilityChoices.PRIVATE)
    remark = models.TextField("备注", blank=True)
    import_batch = models.ForeignKey(
        ExpenseImportBatch,
        verbose_name="导入批次",
        on_delete=models.SET_NULL,
        related_name="records",
        null=True,
        blank=True,
    )
    import_row_number = models.PositiveIntegerField("源文件行号", null=True, blank=True)
    import_fingerprint = models.CharField("导入去重指纹", max_length=64, null=True, blank=True)
    extra_data = models.JSONField("扩展字段", default=dict, blank=True)

    class Meta:
        verbose_name = "支出记录"
        verbose_name_plural = "支出记录"
        indexes = [
            models.Index(fields=["family", "member", "expense_date"]),
            models.Index(fields=["family", "member", "period_start", "period_end"]),
            models.Index(fields=["category"]),
            models.Index(fields=["bank_account"]),
            models.Index(fields=["import_batch", "import_row_number"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["family", "import_fingerprint"],
                condition=models.Q(import_fingerprint__isnull=False),
                name="unique_imported_expense_fingerprint",
            ),
        ]

    def __str__(self):
        return f"{self.expense_date} {self.member} {self.amount}"


class CashflowMonthlySummary(TimestampedModel):
    family = models.ForeignKey(Family, verbose_name="所属家庭", on_delete=models.CASCADE, related_name="cashflow_summaries")
    member = models.ForeignKey(FamilyMember, verbose_name="所属成员", on_delete=models.CASCADE, related_name="cashflow_summaries", null=True, blank=True)
    year = models.PositiveIntegerField("年")
    month = models.PositiveIntegerField("月")
    total_income = models.DecimalField("总收入", max_digits=20, decimal_places=4, default=0)
    total_expense = models.DecimalField("总支出", max_digits=20, decimal_places=4, default=0)
    net_cashflow = models.DecimalField("净现金流", max_digits=20, decimal_places=4, default=0)
    currency = models.CharField("币种", max_length=10, default="CNY")
    extra_data = models.JSONField("扩展字段", default=dict, blank=True)

    class Meta:
        verbose_name = "月度现金流汇总"
        verbose_name_plural = "月度现金流汇总"
        indexes = [
            models.Index(fields=["family", "member", "year", "month"]),
        ]
        constraints = [
            models.UniqueConstraint(fields=["family", "member", "year", "month", "currency"], name="unique_cashflow_monthly_summary")
        ]

    def __str__(self):
        return f"{self.family} {self.year}-{self.month:02d}"


class AnnualBudget(TimestampedModel):
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    family = models.ForeignKey(Family, verbose_name="所属家庭", on_delete=models.CASCADE, related_name="annual_budgets")
    year = models.PositiveIntegerField("预算年度")
    remark = models.TextField("备注", blank=True)
    extra_data = models.JSONField("扩展字段", default=dict, blank=True)

    class Meta:
        verbose_name = "年度预算"
        verbose_name_plural = "年度预算"
        ordering = ["-year", "family__name"]
        constraints = [
            models.UniqueConstraint(fields=["family", "year"], name="unique_annual_budget_per_family_year")
        ]

    def __str__(self):
        return f"{self.family} {self.year}年度预算"


class AnnualBudgetLine(TimestampedModel):
    LINE_TYPE_INCOME = "income"
    LINE_TYPE_EXPENSE = "expense"
    LINE_TYPE_CHOICES = [
        (LINE_TYPE_INCOME, "收入预算"),
        (LINE_TYPE_EXPENSE, "支出预算"),
    ]

    budget = models.ForeignKey(AnnualBudget, verbose_name="年度预算", on_delete=models.CASCADE, related_name="lines")
    line_type = models.CharField("预算类型", max_length=20, choices=LINE_TYPE_CHOICES)
    income_category = models.ForeignKey(IncomeCategory, verbose_name="收入分类", on_delete=models.SET_NULL, null=True, blank=True, related_name="annual_budget_lines")
    expense_category = models.ForeignKey(ExpenseCategory, verbose_name="支出分类", on_delete=models.SET_NULL, null=True, blank=True, related_name="annual_budget_lines")
    annual_amount = models.DecimalField("年度预算金额", max_digits=20, decimal_places=4, default=0)
    remark = models.TextField("备注", blank=True)
    extra_data = models.JSONField("扩展字段", default=dict, blank=True)

    class Meta:
        verbose_name = "年度预算明细"
        verbose_name_plural = "年度预算明细"
        ordering = ["line_type", "income_category__name", "expense_category__name"]
        indexes = [
            models.Index(fields=["budget", "line_type"]),
        ]

    def __str__(self):
        category = self.income_category if self.line_type == self.LINE_TYPE_INCOME else self.expense_category
        return f"{self.get_line_type_display()} {category or '未分类'} {self.annual_amount}"


class AssetBalanceSnapshot(TimestampedModel):
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    family = models.ForeignKey(Family, verbose_name="所属家庭", on_delete=models.CASCADE, related_name="asset_balance_snapshots")
    snapshot_date = models.DateField("快照日期")
    is_draft = models.BooleanField("草稿", default=False)
    base_currency = models.CharField("本位币", max_length=10, default="CNY")
    usd_to_base = models.DecimalField("USD 汇率", max_digits=20, decimal_places=8, default=0)
    hkd_to_base = models.DecimalField("HKD 汇率", max_digits=20, decimal_places=8, default=0)
    title = models.CharField("标题", max_length=120, blank=True)
    remark = models.TextField("备注", blank=True)
    extra_data = models.JSONField("扩展字段", default=dict, blank=True)

    class Meta:
        verbose_name = "资产余额快照"
        verbose_name_plural = "资产余额快照"
        ordering = ["-snapshot_date", "-created_at"]
        indexes = [
            models.Index(fields=["family", "snapshot_date"]),
        ]

    def __str__(self):
        return self.title or f"{self.family} {self.snapshot_date}"


class AssetBalanceEntry(TimestampedModel):
    snapshot = models.ForeignKey(AssetBalanceSnapshot, verbose_name="资产余额快照", on_delete=models.CASCADE, related_name="entries")
    member = models.ForeignKey(FamilyMember, verbose_name="所属成员", on_delete=models.CASCADE, related_name="asset_balance_entries")
    account = models.ForeignKey(BankAccount, verbose_name="账户名称", on_delete=models.SET_NULL, null=True, blank=True, related_name="asset_balance_entries")
    account_name = models.CharField("账户名称备份", max_length=120, blank=True)
    asset_category = models.ForeignKey(AssetCategory, verbose_name="账户资产类别", on_delete=models.SET_NULL, null=True, blank=True, related_name="asset_balance_entries")
    currency = models.CharField("币种", max_length=10, default="CNY")
    original_amount = models.DecimalField("原币余额", max_digits=24, decimal_places=4, default=0)
    base_amount = models.DecimalField("本位币余额", max_digits=24, decimal_places=4, default=0)
    display_order = models.PositiveIntegerField("排序", default=0)
    remark = models.TextField("备注", blank=True)
    extra_data = models.JSONField("扩展字段", default=dict, blank=True)

    class Meta:
        verbose_name = "资产余额明细"
        verbose_name_plural = "资产余额明细"
        ordering = ["display_order", "account__account_name", "asset_category__name", "currency", "member__display_name"]
        indexes = [
            models.Index(fields=["snapshot", "account", "asset_category", "currency"]),
            models.Index(fields=["member"]),
        ]

    def __str__(self):
        account_name = self.account.account_name if self.account else self.account_name
        return f"{self.snapshot} {account_name} {self.asset_category} {self.member}"
