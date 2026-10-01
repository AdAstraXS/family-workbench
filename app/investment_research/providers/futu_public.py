"""Same-security Futu public field definitions, checked against the API figures.

No market-wide field-ID dictionary and no inference from row order. A label is
accepted only when its statement structure, period, currency and rounded value
agree with the API. The original API precision is retained.
"""
import json
import re
import urllib.request
import time
import threading
from decimal import Decimal
from html.parser import HTMLParser
from urllib.parse import urlsplit

from ..number_display import number

SUFFIXES = {1: "income-statement", 2: "balance-sheet", 3: "cash-flow"}
_PUBLIC_LOCK = threading.Lock()
_LAST_PUBLIC_REQUEST = 0


class _Scripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.inside = False
        self.scripts = []
    def handle_starttag(self, tag, attrs):
        self.inside = tag == "script" if not self.inside else self.inside
    def handle_endtag(self, tag):
        if tag == "script":
            self.inside = False
    def handle_data(self, data):
        if self.inside and "window.__INITIAL_STATE__=" in data:
            self.scripts.append(data)


class _Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if newurl != req.full_url:
            raise ValueError("富途页面跳转，字段名称未采用。")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def public_url(code, statement_type):
    if not re.fullmatch(r"(?:US|HK|SH|SZ)\.[A-Za-z0-9.-]{1,20}", code):
        raise ValueError("无效的富途代码")
    market, symbol = code.split(".", 1)
    return f"https://www.futunn.com/stock/{symbol}-{market}/financials-{SUFFIXES[statement_type]}"


def fetch_page(url):
    global _LAST_PUBLIC_REQUEST
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.netloc != "www.futunn.com" or not parts.path.startswith("/stock/"):
        raise ValueError("只允许富途公开财报页面")
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "zh-CN"})
    with _PUBLIC_LOCK:
        delay = 20 - (time.monotonic() - _LAST_PUBLIC_REQUEST)
        if delay > 0:
            time.sleep(delay)
        _LAST_PUBLIC_REQUEST = time.monotonic()
    with urllib.request.build_opener(_Redirect()).open(request, timeout=20) as response:
        raw = response.read(4 * 1024 * 1024 + 1)
    if len(raw) > 4 * 1024 * 1024:
        raise ValueError("富途公开页面超过上限")
    return raw


def _rounded_match(raw_value, expected):
    match = re.fullmatch(r"([+-]?[\d,]+(?:\.\d+)?)(万|亿|万亿)?", str(raw_value or "").strip())
    if not match or expected is None:
        return False
    text, unit = match.groups()
    scale = {None: Decimal(1), "万": Decimal(10000), "亿": Decimal(100000000), "万亿": Decimal(1000000000000)}[unit]
    shown = number(text.replace(",", ""))
    precision = len(text.split(".")[1]) if "." in text else 0
    tolerance = scale * Decimal(10) ** -precision / 2
    return shown is not None and abs(shown * scale - expected) <= tolerance


def enrich_names(code, statement_type, reports, raw, *, verification_reports=None):
    parser = _Scripts()
    parser.feed(raw.decode("utf-8"))
    if len(parser.scripts) != 1:
        raise ValueError("富途页面缺少可核对的财报结构")
    state = json.JSONDecoder().raw_decode(parser.scripts[0].split("window.__INITIAL_STATE__=", 1)[1])[0]
    market, symbol = code.split(".", 1)
    identity = state.get("stock_info", {})
    if str(identity.get("stockCode")) != symbol or identity.get("marketLabel") != market:
        raise ValueError("富途页面与所选证券不一致")
    financial = state.get("financial", {})
    if financial.get("financialSuffix") != "/financials-" + SUFFIXES[statement_type]:
        raise ValueError("富途报表类型不一致")
    definitions = {str(item["id"]): item["name"] for item in financial.get("fieldDefinitions", [])
                   if item.get("id") is not None and item.get("name")}
    evidence, candidates = [], {}
    for report in verification_reports if verification_reports is not None else reports:
        columns = [c for c in financial.get("financialColumns", [])
                   if c.get("label") == report["period"] and c.get("currencyInfo") == report["currency"]]
        if len(columns) != 1:
            continue
        column = columns[0]
        structure = column.get("financialStructure")
        if not isinstance(structure, int):
            continue
        for item in report["items"]:
            # The public structure supplies the high part; individual figures
            # independently verify the relation, including negative/zero values.
            field = str(int(item["field_id"]) - structure * 1000)
            value = (column.get("values", {}).get(field) or {}).get("value")
            if field in definitions and _rounded_match(value, number(item["amount"])):
                name = definitions[field]
                candidates.setdefault(str(item["field_id"]), set()).add(name)
                evidence.append({"field": str(item["field_id"]), "period": report["period"],
                                 "currency": report["currency"], "name": name, "public_value": value})
    # Reject a page which only coincidentally agrees on one or two values.
    if len({e["field"] for e in evidence}) < 3:
        return []
    for report in reports:
        for item in report["items"]:
            names = candidates.get(str(item["field_id"]), set())
            if not item.get("name") and len(names) == 1:
                item["name"] = next(iter(names))
                item["name_source"] = "futu_public_verified"
    return evidence
