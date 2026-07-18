from datetime import timedelta

from django.core.exceptions import ValidationError


MISSING_IPO_ACCOUNTING_DATE_MESSAGE = (
    "该新股资料缺少“招股截止日”和“公布结果日”，"
    "请先到新股资料中补录这两个日期，再进行中签或未中签操作。"
)


def ipo_accounting_date(listing):
    """Return the IPO allotment accounting date without using application date."""
    if listing.allotment_result_date:
        return listing.allotment_result_date
    if listing.subscription_end_date:
        return listing.subscription_end_date + timedelta(days=2)
    raise ValidationError(MISSING_IPO_ACCOUNTING_DATE_MESSAGE)
