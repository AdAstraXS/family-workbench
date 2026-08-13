import hashlib
import html
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone as datetime_timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import urljoin
from xml.etree import ElementTree

from .http_client import SafeHttpError, fetch_with_retries
from .models import IntelligenceSource, SourceItem


MAX_EXCERPT_CHARS = 1800
YOUTUBE_FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"


class FeedParseError(Exception):
    pass


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def _clean_text(value, *, limit=None):
    parser = _TextExtractor()
    try:
        parser.feed(html.unescape(value or ""))
        text = " ".join(parser.parts)
    except Exception:
        text = value or ""
    text = re.sub(r"\s+", " ", text).strip()
    if limit and len(text) > limit:
        return text[: limit - 1].rstrip() + "…"
    return text


def _local_name(tag):
    return tag.rsplit("}", 1)[-1].casefold()


def _children(element, name):
    target = name.casefold()
    return [child for child in list(element) if _local_name(child.tag) == target]


def _first_text(element, *names):
    for name in names:
        matches = _children(element, name)
        if matches:
            text = "".join(matches[0].itertext()).strip()
            if text:
                return text
    return ""


def _parse_datetime(value):
    value = (value or "").strip()
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        parsed = None
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime_timezone.utc)
    return parsed


def _entry_link(entry, base_url):
    for link in _children(entry, "link"):
        href = (link.attrib.get("href") or "").strip()
        rel = (link.attrib.get("rel") or "alternate").casefold()
        if href and rel in {"alternate", ""}:
            return urljoin(base_url, href)
        if link.text and link.text.strip():
            return urljoin(base_url, link.text.strip())
    return ""


def _entry_author(entry):
    direct = _first_text(entry, "author", "creator")
    authors = _children(entry, "author")
    if authors:
        return _first_text(authors[0], "name") or direct
    return direct


def _safe_xml_root(body):
    upper_prefix = body[:4096].upper()
    if b"<!DOCTYPE" in upper_prefix or b"<!ENTITY" in upper_prefix:
        raise FeedParseError("订阅内容包含不允许的 XML 实体声明。")
    try:
        return ElementTree.fromstring(body)
    except ElementTree.ParseError as exc:
        raise FeedParseError("订阅内容不是有效的 RSS/Atom XML。") from exc


@dataclass(frozen=True)
class CollectedItem:
    external_id: str
    title: str
    canonical_url: str
    author_name: str = ""
    published_at: datetime | None = None
    excerpt: str = ""
    language: str = ""
    content_depth: str = SourceItem.DEPTH_TITLE
    raw_metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class AdapterResult:
    items: list[CollectedItem]
    cursor_updates: dict
    not_modified: bool = False


def parse_rss_or_atom(body, *, base_url, max_items=50):
    root = _safe_xml_root(body)
    root_name = _local_name(root.tag)
    if root_name in {"rss", "rdf"}:
        channels = _children(root, "channel")
        container = channels[0] if channels else root
        # RSS 1.0/RDF keeps items beside <channel>, unlike RSS 2.0.
        entries = _children(root, "item") if root_name == "rdf" else _children(container, "item")
        feed_title = _clean_text(_first_text(container, "title"), limit=300)
    elif root_name == "feed":
        container = root
        entries = _children(root, "entry")
        feed_title = _clean_text(_first_text(root, "title"), limit=300)
    else:
        raise FeedParseError("无法识别订阅格式，仅支持 RSS 2.0 或 Atom。")

    collected = []
    for entry in entries[:max_items]:
        title = _clean_text(_first_text(entry, "title"), limit=500)
        link = _entry_link(entry, base_url)
        external_id = _clean_text(_first_text(entry, "guid", "id"), limit=300) or link
        description = _first_text(entry, "description", "summary", "content", "encoded")
        excerpt = _clean_text(description, limit=MAX_EXCERPT_CHARS)
        published_at = _parse_datetime(_first_text(entry, "pubDate", "published", "updated", "date"))
        if not title or not external_id:
            continue
        depth = SourceItem.DEPTH_DESCRIPTION if excerpt else SourceItem.DEPTH_TITLE
        collected.append(
            CollectedItem(
                external_id=external_id,
                title=title,
                canonical_url=link,
                author_name=_clean_text(_entry_author(entry), limit=200),
                published_at=published_at,
                excerpt=excerpt,
                content_depth=depth,
                raw_metadata={"feed_title": feed_title, "format": root_name},
            )
        )
    return collected


def parse_youtube_atom(body, *, expected_channel_id, max_items=50):
    root = _safe_xml_root(body)
    if _local_name(root.tag) != "feed":
        raise FeedParseError("YouTube 频道响应不是有效的 Atom 订阅。")
    feed_channel_id = _first_text(root, "channelId")
    if feed_channel_id and feed_channel_id != expected_channel_id:
        raise FeedParseError("YouTube 订阅返回的频道 ID 与配置不一致。")

    collected = []
    for entry in _children(root, "entry")[:max_items]:
        video_id = _first_text(entry, "videoId")
        title = _clean_text(_first_text(entry, "title"), limit=500)
        link = _entry_link(entry, YOUTUBE_FEED_URL.format(channel_id=expected_channel_id))
        description = ""
        for group in _children(entry, "group"):
            description = _first_text(group, "description")
            if description:
                break
        excerpt = _clean_text(description, limit=MAX_EXCERPT_CHARS)
        if not video_id or not title:
            continue
        collected.append(
            CollectedItem(
                external_id=video_id,
                title=title,
                canonical_url=link or f"https://www.youtube.com/watch?v={video_id}",
                author_name=_clean_text(_entry_author(entry), limit=200),
                published_at=_parse_datetime(_first_text(entry, "published", "updated")),
                excerpt=excerpt,
                content_depth=SourceItem.DEPTH_DESCRIPTION if excerpt else SourceItem.DEPTH_TITLE,
                raw_metadata={
                    "platform": "youtube",
                    "channel_id": feed_channel_id or expected_channel_id,
                    "video_id": video_id,
                    "transcript_status": "not_requested",
                },
            )
        )
    return collected


def _conditional_headers(cursor):
    headers = {}
    if cursor.get("etag"):
        headers["If-None-Match"] = str(cursor["etag"])
    if cursor.get("last_modified"):
        headers["If-Modified-Since"] = str(cursor["last_modified"])
    return headers


def _cursor_updates(response, items):
    updates = {}
    if response.etag:
        updates["etag"] = response.etag
    if response.last_modified:
        updates["last_modified"] = response.last_modified
    dated_items = [item for item in items if item.published_at]
    if dated_items:
        updates["latest_published_at"] = max(item.published_at for item in dated_items).isoformat()
    if items:
        updates["latest_external_id"] = items[0].external_id
    return updates


class RssAdapter:
    key = IntelligenceSource.ADAPTER_RSS

    def collect(self, source, *, max_items=50):
        if not source.url:
            raise SafeHttpError("missing_url", "RSS 信源尚未配置订阅地址。")
        response = fetch_with_retries(source.url, headers=_conditional_headers(source.cursor))
        if response.not_modified:
            return AdapterResult(items=[], cursor_updates={}, not_modified=True)
        items = parse_rss_or_atom(response.body, base_url=response.url, max_items=max_items)
        return AdapterResult(items=items, cursor_updates=_cursor_updates(response, items))


class YouTubeAdapter:
    key = IntelligenceSource.ADAPTER_YOUTUBE

    def collect(self, source, *, max_items=50):
        if not re.fullmatch(r"UC[A-Za-z0-9_-]{22}", source.external_id or ""):
            raise SafeHttpError("invalid_channel_id", "YouTube 信源缺少有效的官方频道 ID。")
        feed_url = YOUTUBE_FEED_URL.format(channel_id=source.external_id)
        response = fetch_with_retries(feed_url, headers=_conditional_headers(source.cursor))
        if response.not_modified:
            return AdapterResult(items=[], cursor_updates={}, not_modified=True)
        items = parse_youtube_atom(
            response.body,
            expected_channel_id=source.external_id,
            max_items=max_items,
        )
        return AdapterResult(items=items, cursor_updates=_cursor_updates(response, items))


ADAPTERS = {
    RssAdapter.key: RssAdapter(),
    YouTubeAdapter.key: YouTubeAdapter(),
}


def get_adapter(source):
    try:
        return ADAPTERS[source.adapter_key]
    except KeyError as exc:
        raise SafeHttpError("unsupported_adapter", f"适配器 {source.adapter_key} 尚未启用。") from exc


def collected_item_fingerprint(item):
    payload = "|".join(
        re.sub(r"\s+", " ", value or "").strip().casefold()
        for value in (item.title, item.canonical_url, item.excerpt)
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
