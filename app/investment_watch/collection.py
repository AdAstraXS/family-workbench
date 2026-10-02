"""Public metadata collection reuses M4.1 HTTP/RSS, without its people classifier."""

import re
from datetime import timedelta, datetime, time
from urllib.parse import urljoin, urldefrag
from django.conf import settings
from django.utils import timezone
from bs4 import BeautifulSoup
from intelligence.http_client import fetch_public_url, SafeHttpError
from intelligence.adapters import _parse_datetime, _clean_text
from investment_research.models import OfficialResearchDocument
from .models import NewsSource
from .services import ingest, WatchError
from .catalogue import qualifies

SOURCES = [
    (
        "wallstreetcn",
        "华尔街见闻 · 公开快讯",
        "https://wallstreetcn.com/live",
        "wallstreetcn",
        "全球",
    ),
    ("huxiu", "虎嗅 · 公开文章", "https://m.huxiu.com/", "huxiu", "中国"),
    (
        "zhitong",
        "智通财经 · 公开列表",
        "https://www.zhitongcaijing.com/",
        "zhitong",
        "全球",
    ),
    (
        "caixin-finance",
        "财新 · 金融公开目录",
        "https://finance.caixin.com/",
        "caixin",
        "中国",
    ),
    (
        "fed",
        "美联储 · 货币政策",
        "https://www.federalreserve.gov/feeds/press_monetary.xml",
        "rss",
        "美国",
    ),
    (
        "microsoft-blog",
        "微软 · 官方博客",
        "https://blogs.microsoft.com/feed/",
        "rss",
        "美国",
    ),
]


def seed_sources(family):
    for key, name, url, adapter, market in SOURCES:
        NewsSource.objects.get_or_create(
            family=family,
            key=key,
            defaults={
                "name": name,
                "url": url,
                "adapter": adapter,
                "market": market,
                "enabled": False,
            },
        )


def parse_list(body, source):
    soup = BeautifulSoup(body, "html.parser")
    prefixes = {
        "wallstreetcn": "/livenews/",
        "huxiu": "https://m.huxiu.com/article/",
        "zhitong": "/content/detail/",
        "caixin": "https://finance.caixin.com/20",
    }
    prefix = prefixes[source.adapter]
    seen = set()
    rows = []
    links = soup.select("a[href]")
    if source.adapter == "zhitong":
        # The carousel repeats list articles without their public excerpts.
        # Parse the complete list cards first, then any carousel-only articles.
        complete = soup.select(".info-list-item a[href]")
        links = complete + [link for link in links if link not in complete]
    for link in links:
        href = link.get("href", "")
        if not href.startswith(prefix):
            continue
        url = urldefrag(urljoin(source.url, href))[0]
        if url in seen:
            continue
        title = _clean_text(link.get_text(" ", strip=True), limit=500)
        if source.adapter == "zhitong":
            heading = link.select_one(".show-text")
            if heading:
                title = _clean_text(heading.get_text(" ", strip=True), limit=500)
        if re.fullmatch(r"评论\s*\(\s*\d+\s*\)", title):
            continue
        container = link.parent
        if source.adapter == "wallstreetcn":
            headline = container.select_one("h2, div.text-black")
            if headline:
                title = _clean_text(headline.get_text(" ", strip=True), limit=500)
        if len(title) < 4:
            continue
        seen.add(url)
        stamp = container.select_one("time[datetime]")
        published = _parse_datetime(stamp.get("datetime", "")) if stamp else None
        # Public list only: no full article download, no paywall fallback.
        summary = _clean_text(container.get_text(" ", strip=True), limit=600)
        if source.adapter == "wallstreetcn":
            summary = re.sub(r"^\d{1,2}:\d{2}\s*", "", summary)
            if summary.startswith(title):
                summary = summary[len(title) :].strip()
            summary = summary or "公开快讯仅提供标题，请查看来源。"
        precision = "time"
        if source.adapter in {"zhitong", "caixin"}:
            wrapper = link.parent.parent
            if source.adapter == "zhitong":
                wrapper = link.find_parent(class_="info-list-item") or wrapper
            excerpt = wrapper.select_one(".info-item-content-desc, p")
            summary = (
                _clean_text(excerpt.get_text(" ", strip=True), limit=600)
                if excerpt
                else "公开目录未提供摘要，请查看来源原文。"
            )
            # Parse only explicit calendar dates. Relative labels are not capture dates.
            match = re.search(
                r"(20\d{2})[年/-](\d{1,2})[月/-](\d{1,2})日?\s+(\d{1,2}):(\d{2})",
                wrapper.get_text(" ", strip=True),
            )
            if match:
                try:
                    published = timezone.make_aware(datetime(*map(int, match.groups())))
                except ValueError:
                    published = None
            elif source.adapter == "caixin" and (
                match := re.search(r"/(20\d{2})-(\d{2})-(\d{2})/", url)
            ):
                try:
                    published = timezone.make_aware(datetime(*map(int, match.groups())))
                    precision = "day"
                except ValueError:
                    published = None
        rows.append(
            {
                "external_id": url,
                "title": title,
                "summary": summary,
                "url": url,
                "published_at": published,
                "published_precision": precision if published else "unknown",
            }
        )
        if len(rows) >= min(source.max_items, 50):
            break
    if not rows:
        raise WatchError("公开列表没有可识别条目，可能是页面结构变更或访问受限。")
    return rows


def collect_source(source, fetcher=fetch_public_url, force=False):
    if (
        not getattr(settings, "INVESTMENT_WATCH_COLLECT_ENABLED", False)
        or not source.enabled
    ):
        return {"status": "disabled", "added": 0}
    now = timezone.now()
    if (
        not force
        and source.last_checked_at
        and now - source.last_checked_at
        < timedelta(minutes=max(15, source.interval_minutes))
    ):
        return {"status": "not_due", "added": 0}
    source.last_checked_at = now
    source.save(update_fields=["last_checked_at", "updated_at"])
    headers = {}
    if source.cursor.get("etag"):
        headers["If-None-Match"] = source.cursor["etag"]
    if source.cursor.get("last_modified"):
        headers["If-Modified-Since"] = source.cursor["last_modified"]
    try:
        response = fetcher(source.url, headers=headers)
        if response.not_modified:
            rows = []
        else:
            from .source_templates import parse_source

            rows = parse_source(response.body, source)
        added = 0
        skipped = 0
        rejected = 0
        for row in rows:
            known = source.materials.filter(
                external_id=str(row.get("external_id", ""))[:500]
            ).exists()
            if not known and ((
                row.get("published_at")
                and row["published_at"] < now - timedelta(days=90)
            ) or not qualifies(row["title"], row["summary"])):
                skipped += 1
                continue
            try:
                _, created = ingest(source, **row)
                added += created
            except WatchError:
                rejected += 1
        if not response.not_modified and source.adapter == "rss":
            from .source_templates import deleted_entries

            for ref in deleted_entries(response.body):
                material = source.materials.filter(external_id=ref).select_related(
                    "current_version"
                ).first()
                if material and material.current_version:
                    old = material.current_version
                    _, created = ingest(
                        source, external_id=material.external_id,
                        title=old.title, summary=old.summary, url=old.url,
                        published_at=old.published_at,
                        published_precision=old.published_precision,
                        occurred_at=old.occurred_at, status="withdrawn",
                    )
                    added += created
        source.last_error = (
            f"{rejected} 条材料校验失败；有效材料已保留，请核对来源模板。"
            if rejected else ""
        )
        if not rejected:
            source.last_success_at = now
        if not response.not_modified and not rejected:
            source.cursor = {
                **source.cursor,
                "etag": response.etag,
                "last_modified": response.last_modified,
            }
        source.consecutive_failures = source.consecutive_failures + 1 if rejected else 0
        source.last_added = added
        source.last_result = "partial" if rejected else "success"
        source.save(
            update_fields=["last_success_at", "last_error", "cursor", "updated_at", "consecutive_failures", "last_added", "last_result"]
        )
        return {
            "status": "partial" if rejected else "success",
            "added": added, "filtered": skipped, "rejected": rejected,
            **({"error": source.last_error} if rejected else {}),
        }
    except Exception as exc:
        source.last_error = (
            exc.safe_message
            if isinstance(exc, SafeHttpError)
            else str(exc)
            if isinstance(exc, WatchError)
            else "解析或网络失败，请检查来源。"
        )[:500]
        source.consecutive_failures += 1
        source.last_result = "failed"
        source.last_added = 0
        source.save(update_fields=["last_error", "updated_at", "consecutive_failures", "last_result", "last_added"])
        return {"status": "failed", "added": 0, "error": source.last_error}


def import_official(family, limit=100):
    """Read existing official documents; never starts SEC/IR downloads."""
    source, _ = NewsSource.objects.get_or_create(
        family=family,
        key="official-research",
        defaults={
            "name": "已归档官方研究资料",
            "url": "https://www.sec.gov/",
            "adapter": "official",
            "market": "美国",
            "enabled": False,
        },
    )
    added = 0
    for document in OfficialResearchDocument.objects.order_by("-updated_at")[:limit]:
        content = (
            document.content_versions.order_by("-version_number")
            .defer("raw_gzip")
            .first()
        )
        text = (content.content_text if content else document.content_text) or ""
        published = (
            timezone.make_aware(datetime.combine(document.published_at, time.min))
            if document.published_at
            else None
        )
        _, created = ingest(
            source,
            external_id=f"official:{document.pk}",
            title=document.title,
            summary=text[:1800],
            url=document.source_url,
            published_at=published,
            official_document=document,
            official_version=content,
            published_precision="day",
        )
        added += created
    return added
