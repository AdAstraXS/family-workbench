"""Bounded public RSS, JSON and static HTML source templates."""

import json
import re
from datetime import datetime
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from django import forms
from django.utils import timezone
from intelligence.adapters import _clean_text, _parse_datetime, parse_rss_or_atom
from .services import WatchError


class SourceForm(forms.Form):
    name = forms.CharField(label="来源名称", max_length=100)
    url = forms.URLField(label="公开列表或订阅地址", max_length=1000)
    adapter = forms.ChoiceField(
        label="读取模板",
        choices=[
            ("rss", "RSS / Atom"),
            ("json", "公开 JSON 接口"),
            ("html", "静态网页列表"),
            ("wallstreetcn", "华尔街见闻"),
            ("zhitong", "智通财经"),
            ("caixin", "财新金融目录"),
            ("huxiu", "虎嗅"),
        ],
    )
    market = forms.ChoiceField(
        label="市场", choices=[(v, v) for v in ["全球", "中国", "美国"]]
    )
    interval_minutes = forms.IntegerField(
        label="检查间隔（分钟）", min_value=15, max_value=10080, initial=120
    )
    max_items = forms.IntegerField(
        label="每轮最多条数", min_value=1, max_value=50, initial=40
    )
    config = forms.JSONField(
        label="模板字段配置",
        required=False,
        initial=dict,
        widget=forms.Textarea(attrs={"rows": 5}),
    )

    def clean_url(self):
        url = self.cleaned_data["url"]
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or parts.username or parts.password:
            raise forms.ValidationError("仅支持无登录凭据的公开 HTTP / HTTPS 地址。")
        return url

    def clean(self):
        data = super().clean()
        config = data.get("config") or {}
        if not isinstance(config, dict) or len(json.dumps(config)) > 4000:
            raise forms.ValidationError("配置须为不超过 4000 字符的 JSON 对象。")
        allowed = {"items", "title", "url", "summary", "date", "status"}
        if set(config) - allowed or any(
            not isinstance(v, str) or len(v) > 200 for v in config.values()
        ):
            raise forms.ValidationError(
                "仅支持 items、title、url、summary、date、status 字符串字段。"
            )
        if data.get("adapter") in {"json", "html"} and not all(
            config.get(k) for k in ("items", "title", "url")
        ):
            raise forms.ValidationError(
                "网页和接口模板需要 items、title、url 字段配置。"
            )
        data["config"] = config
        return data


def field(value, path):
    for part in path.split(".") if path else []:
        if not isinstance(value, dict):
            return ""
        value = value.get(part, "")
    return value


def published_date(raw):
    """Date-only metadata must not appear as an invented publication time."""
    raw = str(raw or "").strip()
    precision = "day" if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw) else "time"
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        value = _parse_datetime(raw)
    if value and timezone.is_naive(value):
        value = timezone.make_aware(value)
    return value, precision if value else "unknown"


def deleted_entries(body):
    """Only explicit Atom tombstones withdraw previously collected entries."""
    from intelligence.adapters import _safe_xml_root

    root = _safe_xml_root(body)
    return [
        node.get("ref")
        for node in root.findall("{http://purl.org/atompub/tombstones/1.0}deleted-entry")
        if node.get("ref")
    ][:50]


def parse_source(body, source):
    if source.adapter == "rss":
        return [
            {
                "external_id": r.external_id,
                "title": r.title,
                "summary": r.excerpt,
                "url": r.canonical_url,
                "published_at": r.published_at,
            }
            for r in parse_rss_or_atom(
                body, base_url=source.url, max_items=min(source.max_items, 50)
            )
        ]
    if source.adapter not in {"json", "html"}:
        from .collection import parse_list

        if source.adapter not in {"wallstreetcn", "huxiu", "zhitong", "caixin"}:
            raise WatchError("此来源没有可用解析器。")
        return parse_list(body, source)
    config = source.config
    try:
        if source.adapter == "json":
            items = field(json.loads(body), config["items"])
            if not isinstance(items, list):
                raise ValueError("items")
        else:
            items = BeautifulSoup(body, "html.parser").select(config["items"])
        rows = []
        for item in items[: min(source.max_items, 50)]:

            def extract(key):
                path = config.get(key, "")
                if not path:
                    return ""
                if source.adapter == "json":
                    value = field(item, path)
                    return str(value) if isinstance(value, (str, int, float)) else ""
                selector, sep, attribute = path.partition("@")
                node = item.select_one(selector.strip()) if selector.strip() else item
                return (
                    (node.get(attribute, "") if sep else node.get_text(" ", strip=True))
                    if node
                    else ""
                )

            title = _clean_text(extract("title"), limit=500)
            link = extract("url")
            url = urljoin(source.url, link)
            if not title or not link or urlsplit(url).scheme not in {"http", "https"}:
                continue
            published, precision = published_date(extract("date"))
            rows.append(
                {
                    "external_id": url,
                    "title": title,
                    "summary": _clean_text(extract("summary"), limit=1800),
                    "url": url,
                    "published_at": published,
                    "published_precision": precision,
                    "status": extract("status").strip().casefold() or "active",
                }
            )
        if not rows:
            raise WatchError("没有读取到文章，请检查模板字段或网页是否需要动态加载。")
        return rows
    except WatchError:
        raise
    except Exception as exc:
        raise WatchError("模板解析失败，请检查 JSON 路径或 CSS 选择器。") from exc
