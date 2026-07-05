import json
from datetime import datetime
from decimal import Decimal
from urllib.request import Request, urlopen

from django.db import transaction
from django.utils import timezone

from family_core.models import ExchangeRate

from .models import DailyExchangeRateFetch


CHINAMONEY_URL = (
    "https://www.chinamoney.com.cn/"
    "r/cms/www/chinamoney/data/fx/ccpr.json"
)


def _result(log):
    rates = {
        item.base_currency: item.rate
        for item in ExchangeRate.objects.filter(
            base_currency__in=["USD", "HKD"],
            quote_currency="CNY",
            rate_date=log.source_date,
        )
    } if log.source_date else {}
    return {
        "today": log.fetch_date,
        "source_date": log.source_date,
        "usd_cny": rates.get("USD"),
        "hkd_cny": rates.get("HKD"),
        "status": log.status,
        "error": log.error_message,
        "source_url": "https://www.chinamoney.com.cn/chinese/bkccpr/",
    }


def ensure_daily_exchange_rates():
    today = timezone.localdate()
    log, should_fetch = DailyExchangeRateFetch.objects.get_or_create(
        fetch_date=today,
        defaults={"status": "fetching"},
    )
    if not should_fetch:
        return _result(log)

    try:
        request = Request(
            CHINAMONEY_URL,
            headers={"User-Agent": "Mozilla/5.0 FamilyFinance/1.0"},
        )
        with urlopen(request, timeout=8) as response:
            payload = json.loads(response.read().decode("utf-8"))
        source_date = datetime.strptime(
            payload["data"]["lastDate"].split()[0],
            "%Y-%m-%d",
        ).date()
        records = {
            item["vrtEName"]: Decimal(item["price"])
            for item in payload["records"]
        }
        wanted = {"USD": records["USD/CNY"], "HKD": records["HKD/CNY"]}
        with transaction.atomic():
            for currency, rate in wanted.items():
                ExchangeRate.objects.update_or_create(
                    base_currency=currency,
                    quote_currency="CNY",
                    rate_date=source_date,
                    defaults={
                        "rate": rate,
                        "source": "中国货币网人民币汇率中间价",
                        "extra_data": {
                            "fetched_on": today.isoformat(),
                            "url": CHINAMONEY_URL,
                        },
                    },
                )
            log.source_date = source_date
            log.status = "success"
            log.error_message = ""
            log.save(
                update_fields=[
                    "source_date",
                    "status",
                    "error_message",
                    "fetched_at",
                ]
            )
    except Exception as exc:
        log.status = "failed"
        log.error_message = str(exc)[:1000]
        log.save(
            update_fields=["status", "error_message", "fetched_at"],
        )
    return _result(log)
