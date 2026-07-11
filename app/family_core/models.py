import uuid

from django.conf import settings
from django.db import models
from django.utils.text import slugify


class TimestampedModel(models.Model):
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        abstract = True


class Family(TimestampedModel):
    name = models.CharField("家庭名称", max_length=100)
    base_currency = models.CharField("默认本位币", max_length=10, default="CNY")
    remark = models.TextField("备注", blank=True)
    extra_data = models.JSONField("扩展字段", default=dict, blank=True)

    class Meta:
        verbose_name = "家庭"
        verbose_name_plural = "家庭"

    def __str__(self):
        return self.name


class SiteSetting(TimestampedModel):
    household_name = models.CharField("工作台名称", max_length=100, default="家庭工作台")
    base_currency = models.CharField("默认本位币", max_length=10, default="CNY")
    timezone = models.CharField("时区", max_length=50, default="Asia/Shanghai")

    class Meta:
        verbose_name = "站点设置"
        verbose_name_plural = "站点设置"

    def delete(self, *args, **kwargs):
        return None

    @classmethod
    def load(cls):
        instance, _ = cls.objects.get_or_create(pk=1)
        return instance

    def __str__(self):
        return self.household_name


class FamilyMember(TimestampedModel):
    ROLE_ADMIN = "admin"
    ROLE_MEMBER = "member"
    ROLE_VIEWER = "viewer"
    ROLE_CHOICES = [
        (ROLE_ADMIN, "管理员"),
        (ROLE_MEMBER, "家庭成员"),
        (ROLE_VIEWER, "查看者"),
    ]

    family = models.ForeignKey(Family, verbose_name="所属家庭", on_delete=models.CASCADE, related_name="members")
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        verbose_name="登录用户",
        on_delete=models.CASCADE,
        related_name="family_member",
        null=True,
        blank=True,
    )
    display_name = models.CharField("显示名称", max_length=100)
    display_order = models.PositiveIntegerField("显示顺序", default=0)
    role = models.CharField("角色", max_length=20, choices=ROLE_CHOICES, default=ROLE_MEMBER)
    is_active = models.BooleanField("是否有效", default=True)
    remark = models.TextField("备注", blank=True)
    extra_data = models.JSONField("扩展字段", default=dict, blank=True)

    class Meta:
        verbose_name = "家庭成员"
        verbose_name_plural = "家庭成员"
        ordering = ["display_order", "id"]
        indexes = [
            models.Index(fields=["family", "role"]),
            models.Index(fields=["is_active"]),
        ]

    def __str__(self):
        return self.display_name


class Currency(models.Model):
    code = models.CharField("币种代码", max_length=10, unique=True)
    name = models.CharField("币种名称", max_length=50)
    symbol = models.CharField("符号", max_length=10, blank=True)
    is_active = models.BooleanField("是否启用", default=True)

    class Meta:
        verbose_name = "币种"
        verbose_name_plural = "币种"
        ordering = ["code"]

    def __str__(self):
        return f"{self.code} - {self.name}"


class ExchangeRate(models.Model):
    base_currency = models.CharField("基准币种", max_length=10)
    quote_currency = models.CharField("目标币种", max_length=10)
    rate = models.DecimalField("汇率", max_digits=20, decimal_places=8)
    rate_date = models.DateField("日期")
    source = models.CharField("数据来源", max_length=100, blank=True)
    extra_data = models.JSONField("扩展字段", default=dict, blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        verbose_name = "汇率"
        verbose_name_plural = "汇率"
        indexes = [
            models.Index(fields=["base_currency", "quote_currency", "rate_date"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["base_currency", "quote_currency", "rate_date"],
                name="unique_exchange_rate_per_day",
            )
        ]

    def __str__(self):
        return f"{self.base_currency}/{self.quote_currency} {self.rate_date}"


class BaseLookup(TimestampedModel):
    family = models.ForeignKey(Family, verbose_name="所属家庭", on_delete=models.CASCADE, null=True, blank=True)
    name = models.CharField("名称", max_length=100)
    display_order = models.PositiveIntegerField("排序", default=0)
    is_active = models.BooleanField("是否启用", default=True)
    remark = models.TextField("备注", blank=True)
    extra_data = models.JSONField("扩展字段", default=dict, blank=True)

    class Meta:
        abstract = True
        ordering = ["display_order", "name"]

    def __str__(self):
        return self.name


class AccountType(BaseLookup):
    code = models.SlugField("稳定代码", max_length=50, blank=True)

    class Meta(BaseLookup.Meta):
        verbose_name = "账户类型"
        verbose_name_plural = "账户类型"
        constraints = [
            models.UniqueConstraint(fields=["family", "name"], name="unique_account_type_per_family"),
            models.UniqueConstraint(fields=["family", "code"], name="unique_account_type_code_per_family"),
        ]

    def save(self, *args, **kwargs):
        if not self.code:
            known_codes = {
                "银行": "bank",
                "券商": "broker",
                "支付宝": "alipay",
                "微信": "wechat",
                "养老金": "pension",
            }
            self.code = (
                known_codes.get(self.name)
                or slugify(self.name)
                or f"account-type-{uuid.uuid4().hex[:12]}"
            )
        super().save(*args, **kwargs)


class AssetCategory(BaseLookup):
    class Meta(BaseLookup.Meta):
        verbose_name = "资产类别"
        verbose_name_plural = "资产类别"
        constraints = [
            models.UniqueConstraint(fields=["family", "name"], name="unique_asset_category_per_family"),
        ]


class AccountRegion(BaseLookup):
    class Meta(BaseLookup.Meta):
        verbose_name = "账户地区"
        verbose_name_plural = "账户地区"
        constraints = [
            models.UniqueConstraint(fields=["family", "name"], name="unique_account_region_per_family"),
        ]
