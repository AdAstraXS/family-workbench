import base64
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from html import unescape
import http.client
import io
import json
import logging
import os
import re
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request

from django.utils import timezone

from ai_analysis.models import AiProvider
from .models import HkIpoListing, HkIpoListingOption


logger = logging.getLogger(__name__)


class IpoImageRecognitionError(Exception):
    pass


DEFAULT_API_KEY_ENV_VARS = (
    "ZHIPU_API_KEY",
    "ARK_API_KEY",
    "VOLCENGINE_API_KEY",
    "OPENAI_API_KEY",
    "AI_API_KEY",
)
SENSITIVE_PROVIDER_EXTRA_KEYS = {"api_key", "apikey", "secret_key", "access_token", "token"}


FIELD_SCHEMA = {
    "stock_code": "股票代码，例如 066xx.HK",
    "stock_name": "股票名称，汉字缩写名称",
    "company_name": "公司名称",
    "listing_type": "类型，可选 new_listing, ah, us_hk, gem, other",
    "mechanism": "机制，可选 a, b, 18a, 18c",
    "subscription_start_date": "招股开始日，YYYY-MM-DD",
    "subscription_end_date": "招股截止日，YYYY-MM-DD",
    "allotment_result_date": "公布结果日，YYYY-MM-DD",
    "listing_date": "上市日期，YYYY-MM-DD",
    "offer_price_min": "招股价下限，数字",
    "offer_price_max": "招股价上限，数字",
    "final_price": "最终定价，数字",
    "lot_size": "每手股数，整数",
    "global_offer_shares_10k": "全球发售股数，单位万股，数字",
    "total_market_cap_100m": "发行后总市值，单位亿港元，数字",
    "h_share_market_cap_100m": "H股市值，单位亿港元，数字",
    "sector": "板块",
    "business_summary": "主要业务",
    "sponsor": "保荐人",
    "has_sponsor_dealer": "是否有庄家，可选 yes, likely, unknown, market",
    "has_greenshoe": "绿鞋，布尔值",
    "stabilizing_manager": "稳价人",
    "has_offer_size_adjustment": "发售量调整权，布尔值",
    "offer_size_adjustment_pct": "发售量调整比例，百分比数字",
    "has_cornerstone": "是否有基石投资者，布尔值",
    "cornerstone_investors": "基石投资者名单",
    "cornerstone_pct": "基石占比，百分比数字",
    "pe_ratio": "市盈率 PE，亏损可填亏损",
    "ps_ratio": "市销率 PS，数字",
    "comparable_companies": "同行业可比公司",
    "valuation_comment": "估值评价，可选 low, reasonable, high, expensive",
    "fundamentals_score": "基本面评分，1-5",
    "heat_score": "热度评分，1-5",
    "subscription_recommendation": "认购建议，可选 skip, cash_one_lot, margin_one_lot, pool_a, pool_b",
    "decision_reason": "决策理由",
    "remark": "备注",
}

VBKR_IPO_URL = "https://www.vbkr.com/ipo/hk/v2/ipo-hk-index"
DNS_OVER_HTTPS_URL = "https://doh.pub/dns-query"
_vbkr_margin_cache = {"fetched_at": None, "data": {}}
JESSE_LIVERMORE_IPO_URL = "https://www.jesselivermore.com/ipo.html"
JESSE_LIVERMORE_IPO_API_URL = (
    "https://h5stockserver.huanshoulv.com/aimapp/hkstock/newStockSearch"
)
HK_CONNECT_THRESHOLD_URL = (
    "https://cloudapi.livereport8.com/northsouthentrycheck/"
    "NorthSouthEntryCheckNew/GetNorthCheckSummary"
)
_hk_connect_threshold_cache = {
    "fetched_at": None,
    "value": None,
    "check_date": None,
}

CHOICE_ALIASES = {
    "listing_type": {
        "新上市": "new_listing",
        "AH": "ah",
        "A H": "ah",
        "美港": "us_hk",
        "创业板": "gem",
        "其他": "other",
    },
    "mechanism": {
        "机制A": "a",
        "机制B": "b",
        "18A": "18a",
        "18C": "18c",
    },
    "has_sponsor_dealer": {
        "有": "yes",
        "大概率": "likely",
        "不确定": "unknown",
        "市场化": "market",
    },
    "valuation_comment": {
        "偏低": "low",
        "合理": "reasonable",
        "偏高": "high",
        "很贵": "expensive",
    },
    "subscription_recommendation": {
        "不认购": "skip",
        "现金一手": "cash_one_lot",
        "融资一手": "margin_one_lot",
        "甲组": "pool_a",
        "乙组": "pool_b",
    },
}

BOOLEAN_ALIASES = {
    "是": True,
    "有": True,
    "true": True,
    "yes": True,
    "1": True,
    "否": False,
    "无": False,
    "false": False,
    "no": False,
    "0": False,
}


def get_vision_providers():
    return (
        AiProvider.objects.filter(
            is_active=True,
            provider_type__in=["openai", "openai_compatible", "vision"],
        )
        .exclude(model_name__in=["", "待配置"])
        .order_by("name", "model_name")
    )


def get_active_vision_provider(provider_id=None):
    providers = get_vision_providers()
    if provider_id not in (None, ""):
        try:
            provider = providers.get(pk=int(provider_id))
        except (TypeError, ValueError, AiProvider.DoesNotExist) as exc:
            raise IpoImageRecognitionError("所选图片识别服务不可用，请刷新页面后重试。") from exc
    else:
        provider = providers.order_by("-updated_at").first()
    if not provider:
        raise IpoImageRecognitionError("未找到已启用的 AI 视觉服务商，请先在后台配置 AI 服务商。")
    return provider


def get_api_key(provider):
    extra_data = provider.extra_data or {}
    configured_env_var = extra_data.get("api_key_env_var")
    env_var_names = [configured_env_var] if configured_env_var else []
    env_var_names.extend(name for name in DEFAULT_API_KEY_ENV_VARS if name not in env_var_names)
    api_key = next((os.getenv(name) for name in env_var_names if name and os.getenv(name)), None)
    if not api_key:
        unsafe_keys = sorted(SENSITIVE_PROVIDER_EXTRA_KEYS.intersection(extra_data))
        if unsafe_keys:
            raise IpoImageRecognitionError(
                "AI 服务商 API Key 不能保存在数据库 extra_data 中。"
                "请迁移到环境变量，"
                "并删除 extra_data 中的敏感字段。"
            )
        expected_env_var = configured_env_var or " / ".join(DEFAULT_API_KEY_ENV_VARS)
        raise IpoImageRecognitionError(
            f"{provider.name} 未配置 API Key。请设置环境变量 {expected_env_var}；"
            "如需自定义变量名，可在服务商 extra_data.api_key_env_var 中填写。"
        )
    return api_key


def get_chat_completions_url(provider):
    base_url = (provider.base_url or "https://api.openai.com/v1").rstrip("/")
    if base_url.endswith("/chat/completions"):
        return base_url
    return f"{base_url}/chat/completions"


def _resolve_ipv4_with_doh(hostname):
    query = urllib.parse.urlencode({"name": hostname, "type": "A"})
    request = urllib.request.Request(
        f"{DNS_OVER_HTTPS_URL}?{query}",
        headers={
            "Accept": "application/dns-json",
            "User-Agent": "FamilyWorkbench/1.0",
        },
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=8) as response:
        payload = json.loads(response.read().decode("utf-8"))
    for answer in payload.get("Answer", []):
        address = str(answer.get("data", "")).strip()
        if answer.get("type") == 1:
            socket.inet_aton(address)
            return address
    raise OSError(f"DoH 未返回 {hostname} 的 IPv4 地址")


def _read_https_via_ipv4(request, ipv4_address, timeout):
    parsed = urllib.parse.urlsplit(request.full_url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("IPv4 回退仅支持 HTTPS URL")

    port = parsed.port or 443
    connection = http.client.HTTPSConnection(parsed.hostname, port, timeout=timeout)
    raw_socket = socket.create_connection((ipv4_address, port), timeout=timeout)
    connection.sock = ssl.create_default_context().wrap_socket(
        raw_socket,
        server_hostname=parsed.hostname,
    )
    path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    headers = dict(request.header_items())
    headers.setdefault("Host", parsed.netloc)
    try:
        connection.request(
            request.get_method(),
            path,
            body=request.data,
            headers=headers,
        )
        response = connection.getresponse()
        body = response.read()
        if response.status >= 400:
            raise urllib.error.HTTPError(
                request.full_url,
                response.status,
                response.reason,
                response.headers,
                io.BytesIO(body),
            )
        return body
    finally:
        connection.close()


def _read_url_with_ipv4_doh_fallback(request, timeout):
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError:
        raise
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        hostname = urllib.parse.urlsplit(request.full_url).hostname
        if not hostname:
            raise
        logger.warning(
            "Direct request to %s failed, retrying through DoH IPv4 resolution: %s",
            hostname,
            exc,
        )
        ipv4_address = _resolve_ipv4_with_doh(hostname)
        return _read_https_via_ipv4(request, ipv4_address, timeout)


def strip_json_markdown(text):
    text = text.strip()
    match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    return text


def normalize_value(field_name, value):
    if value in (None, ""):
        return ""
    configurable_categories = {
        "listing_type": HkIpoListingOption.CATEGORY_LISTING_TYPE,
        "mechanism": HkIpoListingOption.CATEGORY_MECHANISM,
    }
    if field_name in configurable_categories:
        aliases = {
            alias: code
            for code, name in HkIpoListingOption.choices_for(
                configurable_categories[field_name]
            )
            for alias in (code, name)
        }
        normalized = aliases.get(str(value).strip())
        if normalized:
            return normalized
    if field_name in CHOICE_ALIASES:
        normalized = CHOICE_ALIASES[field_name].get(str(value).strip())
        return normalized or str(value).strip()
    if field_name in {"has_greenshoe", "has_offer_size_adjustment", "has_cornerstone"}:
        if isinstance(value, bool):
            return value
        return BOOLEAN_ALIASES.get(str(value).strip().lower(), bool(value))
    return value


def normalize_recognized_fields(raw_data):
    fields = {}
    for field_name in FIELD_SCHEMA:
        if field_name not in raw_data:
            continue
        fields[field_name] = normalize_value(field_name, raw_data.get(field_name))
    return fields


def build_prompt():
    field_schema = FIELD_SCHEMA.copy()
    field_schema["listing_type"] = (
        "类型，可选 "
        + ", ".join(
            f"{code}（{name}）"
            for code, name in HkIpoListingOption.choices_for(
                HkIpoListingOption.CATEGORY_LISTING_TYPE
            )
        )
    )
    field_schema["mechanism"] = (
        "机制，可选 "
        + ", ".join(
            f"{code}（{name}）"
            for code, name in HkIpoListingOption.choices_for(
                HkIpoListingOption.CATEGORY_MECHANISM
            )
        )
    )
    return (
        "你是港股打新资料录入助手。请从图片中识别新股招股资料，并只返回 JSON 对象。\n"
        "不要返回解释，不要使用 Markdown。未识别字段请省略，不要编造。\n"
        "只能填写图片中清晰可见或可直接计算的内容，严禁根据常识、示例、历史新股资料或猜测补全。\n"
        "如果图片为空白、模糊、不是新股资料，或无法识别出股票代码/公司名称等核心信息，请返回空对象 {}。\n"
        "日期统一为 YYYY-MM-DD；金额、比例、股数只返回数字，不要带单位或逗号。\n"
        "字段说明如下：\n"
        f"{json.dumps(field_schema, ensure_ascii=False, indent=2)}"
    )


def recognize_ipo_listing_from_image(uploaded_file, provider_id=None):
    provider = get_active_vision_provider(provider_id)
    api_key = get_api_key(provider)
    model_name = provider.model_name
    if not model_name or model_name == "待配置":
        raise IpoImageRecognitionError("AI 服务商未配置可用模型名称。")

    image_bytes = uploaded_file.read()
    if not image_bytes:
        raise IpoImageRecognitionError("上传的图片为空。")
    mime_type = uploaded_file.content_type or "image/png"
    data_url = f"data:{mime_type};base64,{base64.b64encode(image_bytes).decode('ascii')}"

    image_url = {"url": data_url}
    image_detail = (provider.extra_data or {}).get("image_detail")
    if image_detail in {"low", "high", "xhigh"}:
        image_url["detail"] = image_detail

    payload = {
        "model": model_name,
        "temperature": 0,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": build_prompt()},
                    {"type": "image_url", "image_url": image_url},
                ],
            }
        ],
    }
    url = get_chat_completions_url(provider)
    request_body = json.dumps(payload).encode("utf-8")
    try:
        response_data = None
        for attempt in range(2):
            request = urllib.request.Request(
                url,
                data=request_body,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=45) as response:
                    response_data = json.loads(response.read().decode("utf-8"))
                break
            except urllib.error.HTTPError:
                raise
            except (urllib.error.URLError, TimeoutError) as exc:
                logger.warning("IPO image recognition request failed on attempt %s: %s", attempt + 1, exc)
                if attempt == 1:
                    raise
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        logger.warning("IPO image recognition HTTP error %s: %s", exc.code, error_body[:300])
        raise IpoImageRecognitionError(f"AI 识别请求失败：HTTP {exc.code} {error_body[:300]}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise IpoImageRecognitionError(f"AI 识别请求失败，已重试仍未成功：{exc}") from exc

    try:
        content = response_data["choices"][0]["message"]["content"]
        raw_data = json.loads(strip_json_markdown(content))
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        logger.warning("IPO image recognition returned invalid payload: %s", response_data)
        raise IpoImageRecognitionError("AI 返回内容不是可解析的字段 JSON。") from exc

    return normalize_recognized_fields(raw_data)


def fetch_hk_connect_threshold_100m(force=False):
    fetched_at = _hk_connect_threshold_cache["fetched_at"]
    if (
        not force
        and fetched_at
        and timezone.now() - fetched_at < timedelta(hours=6)
    ):
        return _hk_connect_threshold_cache["value"]

    request = urllib.request.Request(
        HK_CONNECT_THRESHOLD_URL,
        headers={"Accept": "application/json", "User-Agent": "FamilyWorkbench/1.0"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            payload = json.loads(response.read().decode("utf-8"))
        threshold_hkd = payload.get("data", {}).get("inThreshold")
        threshold = Decimal(str(threshold_hkd)) / Decimal("100000000")
        if payload.get("result") != 1 or threshold <= 0:
            raise ValueError("invalid threshold response")
    except (
        urllib.error.URLError,
        TimeoutError,
        InvalidOperation,
        ValueError,
        TypeError,
        json.JSONDecodeError,
    ) as exc:
        logger.warning("HK Connect threshold fetch failed: %s", exc)
        return _hk_connect_threshold_cache["value"]

    _hk_connect_threshold_cache.update(
        {
            "fetched_at": timezone.now(),
            "value": threshold,
            "check_date": payload.get("data", {}).get("checkDate"),
        }
    )
    return threshold


def refresh_hk_connect_threshold():
    threshold = fetch_hk_connect_threshold_100m()
    if threshold is None:
        return None
    HkIpoListing.objects.exclude(hk_connect_threshold_100m=threshold).update(
        hk_connect_threshold_100m=threshold
    )
    return threshold


def fetch_vbkr_expected_margin_multiples():
    fetched_at = _vbkr_margin_cache["fetched_at"]
    if fetched_at and timezone.now() - fetched_at < timedelta(minutes=15):
        return _vbkr_margin_cache["data"]

    request = urllib.request.Request(
        VBKR_IPO_URL,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept-Language": "zh-HK,zh;q=0.9,en;q=0.8",
        },
        method="GET",
    )
    try:
        html = _read_url_with_ipv4_doh_fallback(request, timeout=12).decode(
            "utf-8",
            errors="replace",
        )
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        logger.warning("VBKR expected margin fetch failed: %s", exc)
        return _vbkr_margin_cache["data"]

    text = re.sub(r"<[^>]+>", " ", html)
    text = unescape(re.sub(r"\s+", " ", text))
    code_matches = list(re.finditer(r"\b\d{5}\.HK\b", text))
    data = {}
    for index, match in enumerate(code_matches):
        code = match.group(0)
        next_start = code_matches[index + 1].start() if index + 1 < len(code_matches) else len(text)
        block = text[match.end():next_start]
        multiples = re.findall(r"\d[\d,]*(?:\.\d+)?倍", block)
        if not multiples:
            continue
        expected_margin = multiples[-1]
        data[code] = expected_margin
        data[code.replace(".HK", "")] = expected_margin
    if not data:
        logger.warning("VBKR expected margin fetch returned no recognizable margin data.")

    _vbkr_margin_cache["fetched_at"] = timezone.now()
    _vbkr_margin_cache["data"] = data
    return data


def get_cached_vbkr_expected_margin_multiples():
    return _vbkr_margin_cache["data"]


def _normalize_hk_stock_code(value):
    code = str(value or "").strip().upper()
    code = code.removesuffix(".HK")
    digits = re.sub(r"\D", "", code)
    return digits.zfill(5) if digits else ""


def _livermore_decimal(value):
    if value in (None, "", "-"):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _livermore_date(value):
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def fetch_jesselivermore_ipo_metrics(year):
    if isinstance(year, (tuple, list)):
        start_year, end_year = year
    else:
        start_year = end_year = year
    query = urllib.parse.urlencode(
        {
            "page": 1,
            "page_count": 1000,
            "sort_field_name": "issue_date",
            "sort_type": -1,
            "issue_year": f"{start_year},{end_year}",
        }
    )
    request = urllib.request.Request(
        f"{JESSE_LIVERMORE_IPO_API_URL}?{query}",
        headers={
            "Accept": "application/json",
            "Referer": JESSE_LIVERMORE_IPO_URL,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if str(payload.get("status")) != "200":
            raise ValueError("unexpected API status")
        data = payload.get("data") or {}
        fields = data.get("fields") or []
        rows = data.get("list") or []
        indexes = {name: index for index, name in enumerate(fields)}
        required_fields = {
            "stock_code",
            "issue_date",
            "industry",
            "over_subscribed_multiple",
            "offering_price",
            "px_open_rate",
            "px_close_rate",
            "inception_px_change_rate",
        }
        if not required_fields.issubset(indexes):
            raise ValueError("missing expected IPO fields")
    except (
        urllib.error.URLError,
        TimeoutError,
        ValueError,
        TypeError,
        json.JSONDecodeError,
    ) as exc:
        logger.warning("Jesse Livermore IPO metrics fetch failed: %s", exc)
        return {}

    metrics = {}
    for row in rows:
        if not isinstance(row, list):
            continue

        def value(field_name):
            index = indexes[field_name]
            return row[index] if index < len(row) else None

        stock_code = _normalize_hk_stock_code(value("stock_code"))
        if not stock_code:
            continue
        industry = str(value("industry") or "").strip()
        metrics[stock_code] = {
            "listing_date": _livermore_date(value("issue_date")),
            "industry": "" if industry == "-" else industry,
            "over_subscription_multiple": _livermore_decimal(
                value("over_subscribed_multiple")
            ),
            "final_price": _livermore_decimal(value("offering_price")),
            "first_day_open_change_pct": _livermore_decimal(
                value("px_open_rate")
            ),
            "first_day_close_change_pct": _livermore_decimal(
                value("px_close_rate")
            ),
            "cumulative_change_pct": _livermore_decimal(
                value("inception_px_change_rate")
            ),
        }
    return metrics


def refresh_listed_market_data(listings, year):
    listings = list(listings)
    if not listings:
        return 0
    metrics = fetch_jesselivermore_ipo_metrics(year)
    if not metrics:
        return 0

    static_fields = (
        "listing_date",
        "industry",
        "over_subscription_multiple",
        "final_price",
        "first_day_open_change_pct",
        "first_day_close_change_pct",
    )
    now = timezone.now()
    changed = []
    for listing in listings:
        metric = metrics.get(_normalize_hk_stock_code(listing.stock_code))
        if not metric:
            continue
        if listing.market_data_fetched_at is None:
            for field_name in static_fields:
                value = metric.get(field_name)
                if value not in (None, ""):
                    setattr(listing, field_name, value)
            listing.market_data_fetched_at = now
        else:
            for field_name in static_fields:
                current_value = getattr(listing, field_name)
                value = metric.get(field_name)
                if current_value in (None, "") and value not in (None, ""):
                    setattr(listing, field_name, value)

        cumulative_change = metric.get("cumulative_change_pct")
        if cumulative_change is not None:
            listing.cumulative_change_pct = cumulative_change
        changed.append(listing)

    if changed:
        HkIpoListing.objects.bulk_update(
            changed,
            [
                *static_fields,
                "cumulative_change_pct",
                "market_data_fetched_at",
            ],
        )
    return len(changed)
