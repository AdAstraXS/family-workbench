import base64
from datetime import timedelta
from html import unescape
import json
import logging
import os
import re
import urllib.error
import urllib.request

from django.utils import timezone

from ai_analysis.models import AiProvider


logger = logging.getLogger(__name__)


class IpoImageRecognitionError(Exception):
    pass


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
_vbkr_margin_cache = {"fetched_at": None, "data": {}}

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


def get_active_vision_provider():
    provider = (
        AiProvider.objects.filter(is_active=True)
        .filter(provider_type__in=["openai", "openai_compatible", "vision"])
        .order_by("-updated_at")
        .first()
    )
    if not provider:
        raise IpoImageRecognitionError("未找到已启用的 AI 视觉服务商，请先在后台配置 AI 服务商。")
    return provider


def get_api_key(provider):
    extra_data = provider.extra_data or {}
    api_key = extra_data.get("api_key") or os.getenv("OPENAI_API_KEY") or os.getenv("AI_API_KEY")
    if not api_key:
        raise IpoImageRecognitionError("AI 服务商未配置 API Key。可在环境变量 OPENAI_API_KEY / AI_API_KEY 中配置，或放在服务商 extra_data.api_key。")
    return api_key


def get_chat_completions_url(provider):
    base_url = (provider.base_url or "https://api.openai.com/v1").rstrip("/")
    if base_url.endswith("/chat/completions"):
        return base_url
    return f"{base_url}/chat/completions"


def strip_json_markdown(text):
    text = text.strip()
    match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    return text


def normalize_value(field_name, value):
    if value in (None, ""):
        return ""
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
    return (
        "你是港股打新资料录入助手。请从图片中识别新股招股资料，并只返回 JSON 对象。\n"
        "不要返回解释，不要使用 Markdown。未识别字段请省略，不要编造。\n"
        "只能填写图片中清晰可见或可直接计算的内容，严禁根据常识、示例、历史新股资料或猜测补全。\n"
        "如果图片为空白、模糊、不是新股资料，或无法识别出股票代码/公司名称等核心信息，请返回空对象 {}。\n"
        "日期统一为 YYYY-MM-DD；金额、比例、股数只返回数字，不要带单位或逗号。\n"
        "字段说明如下：\n"
        f"{json.dumps(FIELD_SCHEMA, ensure_ascii=False, indent=2)}"
    )


def recognize_ipo_listing_from_image(uploaded_file):
    provider = get_active_vision_provider()
    api_key = get_api_key(provider)
    model_name = provider.model_name
    if not model_name or model_name == "待配置":
        raise IpoImageRecognitionError("AI 服务商未配置可用模型名称。")

    image_bytes = uploaded_file.read()
    if not image_bytes:
        raise IpoImageRecognitionError("上传的图片为空。")
    mime_type = uploaded_file.content_type or "image/png"
    data_url = f"data:{mime_type};base64,{base64.b64encode(image_bytes).decode('ascii')}"

    payload = {
        "model": model_name,
        "temperature": 0,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": build_prompt()},
                    {"type": "image_url", "image_url": {"url": data_url}},
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
                with urllib.request.urlopen(request, timeout=90) as response:
                    response_data = json.loads(response.read().decode("utf-8"))
                break
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
        html = ""
        for attempt in range(2):
            try:
                with urllib.request.urlopen(request, timeout=12) as response:
                    html = response.read().decode("utf-8", errors="replace")
                break
            except (urllib.error.URLError, TimeoutError) as exc:
                logger.warning("VBKR expected margin fetch failed on attempt %s: %s", attempt + 1, exc)
                if attempt == 1:
                    raise
    except (urllib.error.URLError, TimeoutError):
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
