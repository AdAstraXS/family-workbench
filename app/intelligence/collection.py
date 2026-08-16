import logging
import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from django.db import transaction
from django.utils import timezone

from .adapters import FeedParseError, collected_item_fingerprint, get_adapter
from .http_client import SafeHttpError
from .models import CollectionRun, CollectionRunItem, IntelligenceSource, SourceItem
from .processing import process_source_item


SUPPORTED_ADAPTERS = {
    IntelligenceSource.ADAPTER_RSS,
    IntelligenceSource.ADAPTER_YOUTUBE,
}
logger = logging.getLogger(__name__)
TRACKING_QUERY_KEYS = {
    "fbclid", "gclid", "mc_cid", "mc_eid", "ref",
}


@dataclass
class SourceCollectionResult:
    discovered: int = 0
    created: int = 0
    updated: int = 0
    ignored: int = 0
    normalized: int = 0
    classified: int = 0
    noise: int = 0
    clustered: int = 0
    review: int = 0
    failed: int = 0
    error: str = ""
    cursor_before: dict | None = None
    cursor_after: dict | None = None


def normalize_public_url(url):
    if not url:
        return ""
    parsed = urlsplit(url.strip())
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        return ""
    scheme = parsed.scheme.casefold()
    hostname = parsed.hostname.casefold().rstrip(".")
    try:
        port = parsed.port
    except ValueError:
        return ""
    netloc = hostname
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{hostname}:{port}"
    filtered_query = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        lowered = key.casefold()
        if lowered.startswith("utm_") or lowered in TRACKING_QUERY_KEYS:
            continue
        filtered_query.append((key, value))
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    return urlunsplit((scheme, netloc, path, urlencode(sorted(filtered_query)), ""))


def _item_defaults(collected, fingerprint):
    return {
        "canonical_url": normalize_public_url(collected.canonical_url),
        "title": collected.title,
        "author_name": collected.author_name,
        "published_at": collected.published_at,
        "fetched_at": timezone.now(),
        "language": collected.language,
        "excerpt": collected.excerpt,
        "content_hash": fingerprint,
        "raw_metadata": collected.raw_metadata,
        "content_depth": collected.content_depth,
        "processing_status": SourceItem.STATUS_NORMALIZED,
        "processing_reason": "已完成 URL 标准化和内容指纹计算。",
    }


@transaction.atomic
def _upsert_source_item(source, collected):
    fingerprint = collected_item_fingerprint(collected)
    canonical_url = normalize_public_url(collected.canonical_url)
    item = None
    if collected.external_id:
        item = SourceItem.objects.filter(source=source, external_id=collected.external_id).first()
    if item is None and canonical_url:
        item = SourceItem.objects.filter(source=source, canonical_url=canonical_url).first()
    if item is None:
        item = SourceItem.objects.filter(source=source, content_hash=fingerprint).first()

    defaults = _item_defaults(collected, fingerprint)
    if item is None:
        item = SourceItem.objects.create(
            source=source,
            external_id=collected.external_id,
            **defaults,
        )
        return item, "created"

    changed = any(
        getattr(item, field) != value
        for field, value in defaults.items()
        if field not in {"fetched_at", "processing_status", "processing_reason"}
    )
    item.fetched_at = defaults["fetched_at"]
    if not changed:
        item.save(update_fields=["fetched_at", "updated_at"])
        return item, "ignored"
    for field, value in defaults.items():
        setattr(item, field, value)
    if collected.external_id and not item.external_id:
        item.external_id = collected.external_id
    item.save()
    return item, "updated"


def _mark_source_success(source, cursor):
    source.cursor = cursor
    source.last_success_at = timezone.now()
    source.consecutive_failures = 0
    source.last_error_summary = ""
    source.save(update_fields=[
        "cursor", "last_success_at", "consecutive_failures",
        "last_error_summary", "updated_at",
    ])


def _mark_source_failure(source, message):
    source.consecutive_failures += 1
    source.last_error_summary = message[:500]
    source.save(update_fields=["consecutive_failures", "last_error_summary", "updated_at"])


def collect_one_source(source, *, max_items=50):
    result = SourceCollectionResult(cursor_before=dict(source.cursor or {}))
    source.last_attempt_at = timezone.now()
    source.save(update_fields=["last_attempt_at", "updated_at"])
    try:
        adapter = get_adapter(source)
        adapter_result = adapter.collect(source, max_items=max_items)
        cursor = dict(source.cursor or {})
        cursor.update(adapter_result.cursor_updates)
        cursor["last_checked_at"] = timezone.now().isoformat()
        cursor["last_result"] = "not_modified" if adapter_result.not_modified else "success"

        for collected in adapter_result.items:
            result.discovered += 1
            try:
                item, outcome = _upsert_source_item(source, collected)
                if outcome == "created":
                    result.created += 1
                elif outcome == "updated":
                    result.updated += 1
                else:
                    result.ignored += 1
                    continue
                result.normalized += 1
                processed = process_source_item(item)
                result.classified += 1
                result.noise += int(processed.is_noise)
                result.clustered += processed.event_count
                result.review += processed.event_count
            except Exception:
                logger.exception("Failed to persist or process intelligence source item for source_id=%s", source.pk)
                result.failed += 1
        if result.failed:
            result.error = f"{result.failed} 个条目处理失败；其他条目已继续保存。"
            _mark_source_failure(source, result.error)
            partial_cursor = dict(source.cursor or {})
            partial_cursor["last_checked_at"] = timezone.now().isoformat()
            partial_cursor["last_result"] = "partial"
            result.cursor_after = partial_cursor
        else:
            _mark_source_success(source, cursor)
            result.cursor_after = cursor
        return result
    except (SafeHttpError, FeedParseError) as exc:
        message = exc.safe_message if isinstance(exc, SafeHttpError) else str(exc)
        _mark_source_failure(source, message)
        result.failed = 1
        result.error = message
        result.cursor_after = dict(source.cursor or {})
        return result
    except Exception:
        logger.exception("Unexpected intelligence collection failure for source_id=%s", source.pk)
        message = "采集发生未预期错误；详细堆栈仅保留在服务日志。"
        _mark_source_failure(source, message)
        result.failed = 1
        result.error = message
        result.cursor_after = dict(source.cursor or {})
        return result


def collect_intelligence_sources(
    *,
    source_ids=None,
    due_only=True,
    max_items=50,
    created_by=None,
    family=None,
):
    max_items = max(1, min(int(max_items), 100))
    sources = IntelligenceSource.objects.filter(
        is_active=True,
        adapter_key__in=SUPPORTED_ADAPTERS,
    ).prefetch_related("topics")
    if source_ids:
        sources = sources.filter(pk__in=source_ids)
    source_list = [source for source in sources if not due_only or source.is_due]

    run = CollectionRun.objects.create(
        family=family,
        run_kind=CollectionRun.KIND_COLLECTION,
        status=CollectionRun.STATUS_RUNNING,
        parameters={
            "source_ids": sorted(source_ids or []),
            "due_only": bool(due_only),
            "max_items": max_items,
            "supported_adapters": sorted(SUPPORTED_ADAPTERS),
        },
        created_by=created_by,
    )
    source_failures = []
    successful_sources = 0
    for source in source_list:
        result = collect_one_source(source, max_items=max_items)
        per_source_status = CollectionRun.STATUS_SUCCESS
        if result.error and result.discovered:
            per_source_status = CollectionRun.STATUS_PARTIAL
        elif result.error:
            per_source_status = CollectionRun.STATUS_FAILED
        else:
            successful_sources += 1
        if per_source_status == CollectionRun.STATUS_PARTIAL:
            successful_sources += 1
        if result.error:
            source_failures.append(f"{source.name}：{result.error}")
        CollectionRunItem.objects.create(
            run=run,
            source=source,
            status=per_source_status,
            discovered_count=result.discovered,
            created_count=result.created,
            updated_count=result.updated,
            ignored_count=result.ignored,
            noise_count=result.noise,
            clustered_count=result.clustered,
            failed_count=result.failed,
            cursor_before=result.cursor_before or {},
            cursor_after=result.cursor_after or {},
            error_summary=result.error,
        )
        run.discovered_count += result.discovered
        run.created_count += result.created
        run.updated_count += result.updated
        run.ignored_count += result.ignored
        run.normalized_count += result.normalized
        run.classified_count += result.classified
        run.noise_count += result.noise
        run.clustered_count += result.clustered
        run.review_count += result.review
        run.failed_count += result.failed

    if source_failures:
        run.status = CollectionRun.STATUS_PARTIAL if successful_sources else CollectionRun.STATUS_FAILED
    else:
        run.status = CollectionRun.STATUS_SUCCESS
    run.error_summary = "\n".join(source_failures)[:4000]
    run.finished_at = timezone.now()
    run.save()
    return run
