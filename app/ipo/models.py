from django.db import models


class HkIpoListing(models.Model):
    stock_code = models.CharField("股票代码", max_length=20)
    company_name = models.CharField("公司名称", max_length=200)
    listing_date = models.DateField("上市日期", null=True, blank=True)
    status = models.CharField("状态", max_length=30, default="upcoming")
    extra_data = models.JSONField("扩展字段", default=dict, blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        verbose_name = "港股新股"
        verbose_name_plural = "港股新股"

    def __str__(self):
        return f"{self.stock_code} {self.company_name}"
