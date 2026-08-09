import hashlib
import logging
from datetime import timedelta, timezone as datetime_timezone

from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .ai import KnowledgeAiError, generate_proposals
from .content import (
    ONENOTE_CONVERTER_VERSION,
    UnsafeKnowledgeResourceError,
    content_hash,
    extract_resource_references,
    normalize_onenote_html,
    resource_external_id,
    suggested_filename,
    validate_resource_mime,
    validate_resource_signature,
)
from .microsoft import (
    MicrosoftAuthorizationError,
    MicrosoftGraphClient,
    MicrosoftSourceUnavailableError,
)
from .imports import (
    ImportJobCancelled,
    import_knowledge_batch,
    preview_import_batch,
    rollback_knowledge_batch,
)
from .models import (
    KnowledgeAsset,
    KnowledgeDocument,
    KnowledgeImportBatch,
    KnowledgeImportItem,
    KnowledgeJob,
    KnowledgeJobItem,
    KnowledgeProposal,
    KnowledgeRevision,
    KnowledgeSource,
)
from .search import index_document, rebuild_family_search


logger = logging.getLogger(__name__)


class KnowledgeJobCancelled(RuntimeError):
    pass


def _mark_import_batch_failed(job, message):
    batch_id = (job.parameters or {}).get("batch_id")
    if batch_id:
        status = (
            KnowledgeImportBatch.STATUS_PARTIAL
            if KnowledgeImportItem.objects.filter(
                batch_id=batch_id,
                status=KnowledgeImportItem.STATUS_IMPORTED,
            ).exists()
            else KnowledgeImportBatch.STATUS_FAILED
        )
        KnowledgeImportBatch.objects.filter(pk=batch_id).update(
            status=status,
            error_message=str(message)[:4000],
            updated_at=timezone.now(),
        )


def _parse_graph_datetime(value):
    parsed = parse_datetime(str(value or ""))
    if parsed and timezone.is_naive(parsed):
        return timezone.make_aware(parsed, datetime_timezone.utc)
    return parsed


def _safe_source_url(page):
    links = page.get("links") or {}
    url = (links.get("oneNoteWebUrl") or {}).get("href", "")
    return str(url)[:1000] if str(url).startswith("https://") else ""


def _knowledge_status_for_source_route(source, section_id):
    route = source.route_for_section(section_id)
    return {
        KnowledgeSource.ROUTE_ORGANIZE: KnowledgeDocument.KNOWLEDGE_PENDING,
        KnowledgeSource.ROUTE_ARCHIVE: KnowledgeDocument.KNOWLEDGE_ARCHIVED,
    }.get(route, KnowledgeDocument.KNOWLEDGE_INCLUDED)


def _job_item(job, external_id, title, status, error_message="", details=None):
    return KnowledgeJobItem.objects.update_or_create(
        job=job,
        external_id=str(external_id)[:500],
        defaults={
            "title": str(title or "")[:500],
            "status": status,
            "error_message": str(error_message)[:4000],
            "details": details or {},
        },
    )[0]


def queue_knowledge_job(*, family, source, requested_by, job_type, parameters=None):
    parameters = parameters or {}
    try:
        with transaction.atomic():
            return KnowledgeJob.objects.create(
                family=family,
                source=source,
                requested_by=requested_by,
                job_type=job_type,
                parameters=parameters,
            ), True
    except IntegrityError:
        existing = KnowledgeJob.objects.filter(
            source=source,
            job_type=job_type,
            status__in=KnowledgeJob.ACTIVE_STATUSES,
        ).order_by("-created_at").first()
        if existing:
            return existing, False
        raise


def _check_cancelled(job):
    status = KnowledgeJob.objects.filter(pk=job.pk).values_list("status", flat=True).first()
    if status == KnowledgeJob.STATUS_CANCEL_REQUESTED:
        raise KnowledgeJobCancelled("任务已按成员请求取消。")


def _update_job_heartbeat(job, **counters):
    values = {"heartbeat_at": timezone.now(), **counters}
    KnowledgeJob.objects.filter(pk=job.pk).update(**values)
    for key, value in counters.items():
        setattr(job, key, value)
    job.heartbeat_at = values["heartbeat_at"]


def _download_page_resources(client, raw_html):
    downloaded = []
    total_bytes = 0
    references = extract_resource_references(raw_html)
    if len(references) > 100:
        raise UnsafeKnowledgeResourceError("单页附件和图片数量超过 100 个。")
    for reference in references:
        response = client.resource(reference.url)
        mime_type = validate_resource_mime(
            response.content_type or reference.declared_mime,
            reference.is_image,
            response.body,
        )
        validate_resource_signature(response.body, mime_type)
        total_bytes += len(response.body)
        if total_bytes > 100 * 1024 * 1024:
            raise UnsafeKnowledgeResourceError("单页附件总大小超过 100 MB。")
        downloaded.append(
            {
                "reference": reference,
                "body": response.body,
                "mime_type": mime_type,
                "filename": suggested_filename(reference, mime_type),
                "hash": hashlib.sha256(response.body).hexdigest(),
            }
        )
    return downloaded


def _stored_resource_urls(revision, raw_html):
    assets = {asset.external_id: asset for asset in revision.assets.all()}
    resource_urls = {}
    for reference in extract_resource_references(raw_html):
        asset = assets.get(resource_external_id(reference.url))
        if asset is None:
            continue
        protected_url = reverse("knowledge:asset_download", kwargs={"pk": asset.pk})
        resource_urls[reference.url] = protected_url
        for alias in reference.aliases:
            resource_urls[alias] = protected_url
    return resource_urls


def rebuild_document_normalized_content(document, *, save=True):
    revision = document.current_revision
    if revision is None:
        raise ValueError("知识文档没有可重建的当前原始版本。")
    with revision.raw_file.open("rb") as raw_file:
        raw_bytes = raw_file.read()
    raw_html = raw_bytes.decode("utf-8", errors="replace")
    resource_urls = _stored_resource_urls(revision, raw_html)
    safe_html, plain_text = normalize_onenote_html(raw_html, resource_urls)
    changed = any(
        [
            revision.normalized_html != safe_html,
            revision.plain_text != plain_text,
            revision.normalized_hash != content_hash(plain_text.encode("utf-8")),
            revision.converter_version != ONENOTE_CONVERTER_VERSION,
        ]
    )
    if save and changed:
        revision.normalized_html = safe_html
        revision.plain_text = plain_text
        revision.normalized_hash = content_hash(plain_text.encode("utf-8"))
        revision.converter_version = ONENOTE_CONVERTER_VERSION
        revision.save(
            update_fields=[
                "normalized_html",
                "plain_text",
                "normalized_hash",
                "converter_version",
            ]
        )
        document.current_revision = revision
        index_document(document)
    return {
        "changed": changed,
        "plain_text_length": len(plain_text),
        "asset_count": len(resource_urls),
    }


def _save_page_revision(document, raw_bytes, downloaded_resources, source_modified_at):
    raw_hash = content_hash(raw_bytes)
    if (
        document.current_revision_id
        and document.current_revision.content_hash == raw_hash
    ):
        return document.current_revision, False

    next_number = (
        document.revisions.order_by("-revision_number")
        .values_list("revision_number", flat=True)
        .first()
        or 0
    ) + 1
    created_files = []
    try:
        with transaction.atomic():
            revision = KnowledgeRevision.objects.create(
                document=document,
                revision_number=next_number,
                content_hash=raw_hash,
                raw_file="",
                normalized_html="",
                plain_text="",
                converter_version=ONENOTE_CONVERTER_VERSION,
                source_modified_at=source_modified_at,
            )
            revision.raw_file.save(
                "page.html",
                ContentFile(raw_bytes),
                save=True,
            )
            created_files.append(revision.raw_file.name)

            resource_urls = {}
            for item in downloaded_resources:
                reference = item["reference"]
                asset = KnowledgeAsset.objects.create(
                    revision=revision,
                    external_id=resource_external_id(reference.url),
                    original_name=item["filename"],
                    mime_type=item["mime_type"],
                    byte_size=len(item["body"]),
                    content_hash=item["hash"],
                    is_image=reference.is_image,
                    file="",
                )
                asset.file.save(
                    item["filename"],
                    ContentFile(item["body"]),
                    save=True,
                )
                created_files.append(asset.file.name)
                protected_url = reverse("knowledge:asset_download", kwargs={"pk": asset.pk})
                resource_urls[reference.url] = protected_url
                for alias in reference.aliases:
                    resource_urls[alias] = protected_url

            raw_html = raw_bytes.decode("utf-8", errors="replace")
            safe_html, plain_text = normalize_onenote_html(raw_html, resource_urls)
            revision.normalized_html = safe_html
            revision.plain_text = plain_text
            revision.normalized_hash = content_hash(plain_text.encode("utf-8"))
            revision.save(
                update_fields=["normalized_html", "plain_text", "normalized_hash"]
            )

            KnowledgeProposal.objects.filter(
                document=document,
                status=KnowledgeProposal.STATUS_PENDING,
            ).exclude(revision=revision).update(status=KnowledgeProposal.STATUS_STALE)
            document.current_revision = revision
            document.curation_status = KnowledgeDocument.CURATION_NORMALIZED
            document.sync_status = KnowledgeDocument.SYNC_AVAILABLE
            document.source_deleted_at = None
            document.save(
                update_fields=[
                    "current_revision",
                    "curation_status",
                    "sync_status",
                    "source_deleted_at",
                    "updated_at",
                ]
            )
        return revision, True
    except Exception:
        for name in created_files:
            try:
                KnowledgeRevision._meta.get_field("raw_file").storage.delete(name)
            except OSError:
                logger.warning("Unable to remove orphan knowledge file %s", name)
        raise


def _sync_page(client, source, section, page):
    external_id = str(page.get("id", ""))
    if not external_id:
        raise MicrosoftSourceUnavailableError("OneNote 页面缺少稳定 ID。")
    modified_at = _parse_graph_datetime(page.get("lastModifiedDateTime"))
    created_at = _parse_graph_datetime(page.get("createdDateTime"))
    title = str(page.get("title") or "未命名页面")[:500]
    section_name = str(section.get("displayName", ""))[:300]
    section_id = str(section.get("id", ""))
    document, created = KnowledgeDocument.objects.get_or_create(
        source=source,
        external_id=external_id,
        defaults={
            "family": source.family,
            "owner": source.owner,
            "title": title,
            "section_name": section_name,
            "hierarchy": {
                "notebook_id": source.external_id,
                "notebook_name": source.name,
                "section_id": section_id,
                "section_group": (
                    (section.get("parentSectionGroup") or {}).get("displayName", "")
                ),
                "page_level": page.get("level"),
                "page_order": page.get("order"),
            },
            "source_url": _safe_source_url(page),
            "visibility": source.visibility,
            "content_created_at": created_at,
            "content_modified_at": modified_at,
            "curation_status": KnowledgeDocument.CURATION_INBOX,
            "knowledge_status": _knowledge_status_for_source_route(
                source,
                section_id,
            ),
            "library_tier": KnowledgeDocument.LIBRARY_ARCHIVE,
        },
    )
    metadata_changed = any(
        [
            document.title != title,
            document.section_name != section_name,
            document.content_modified_at != modified_at,
            document.sync_status != KnowledgeDocument.SYNC_AVAILABLE,
        ]
    )
    document.title = title
    document.section_name = section_name
    document.hierarchy = {
        "notebook_id": source.external_id,
        "notebook_name": source.name,
        "section_id": section_id,
        "section_group": (
            (section.get("parentSectionGroup") or {}).get("displayName", "")
        ),
        "page_level": page.get("level"),
        "page_order": page.get("order"),
    }
    document.source_url = _safe_source_url(page)
    document.content_created_at = created_at
    document.content_modified_at = modified_at
    document.sync_status = KnowledgeDocument.SYNC_AVAILABLE
    document.source_deleted_at = None
    document.save()

    # OneNote collection metadata can lag behind the page content visible in
    # OneNote Online. Treat lastModifiedDateTime as descriptive metadata only;
    # the downloaded HTML hash is the authoritative change detector.
    raw_bytes = client.page_content(external_id)
    raw_hash = content_hash(raw_bytes)
    if (
        document.current_revision_id
        and document.current_revision.content_hash == raw_hash
    ):
        if document.current_revision.converter_version != ONENOTE_CONVERTER_VERSION:
            rebuild_document_normalized_content(document)
            return document, KnowledgeJobItem.STATUS_UPDATED
        if metadata_changed:
            index_document(document)
        return document, KnowledgeJobItem.STATUS_SKIPPED
    resources = _download_page_resources(
        client,
        raw_bytes.decode("utf-8", errors="replace"),
    )
    _, revision_created = _save_page_revision(
        document,
        raw_bytes,
        resources,
        modified_at,
    )
    index_document(document)
    if created:
        return document, KnowledgeJobItem.STATUS_SUCCESS
    return (
        document,
        KnowledgeJobItem.STATUS_UPDATED
        if revision_created
        else KnowledgeJobItem.STATUS_SKIPPED,
    )


def sync_onenote_source(job):
    source = (
        KnowledgeSource.objects.select_related("connection", "owner", "family")
        .get(pk=job.source_id)
    )
    if source.kind != KnowledgeSource.KIND_ONENOTE or not source.connection_id:
        raise MicrosoftSourceUnavailableError("该来源不是可同步的 OneNote 笔记本。")
    if source.connection.status == source.connection.STATUS_DISCONNECTED:
        raise MicrosoftAuthorizationError("成员已断开 Microsoft 账户。")
    client = MicrosoftGraphClient(source.connection)
    sections = client.sections_for_notebook(source.external_id)
    pages = []
    for section in sections:
        for page in client.pages_for_section(section["id"]):
            pages.append((section, page))

    existing_count = source.documents.count()
    if existing_count and not pages:
        raise MicrosoftSourceUnavailableError(
            "OneNote 返回空笔记本；为避免误判删除，已停止本次对账。"
        )

    job.total_count = len(pages)
    job.save(update_fields=["total_count", "updated_at"])
    seen_ids = set()
    counters = {"success_count": 0, "updated_count": 0, "skipped_count": 0, "failed_count": 0}
    for section, page in pages:
        _check_cancelled(job)
        external_id = str(page.get("id", ""))
        title = str(page.get("title", ""))
        seen_ids.add(external_id)
        try:
            _, status = _sync_page(client, source, section, page)
            _job_item(job, external_id, title, status)
            if status == KnowledgeJobItem.STATUS_SUCCESS:
                counters["success_count"] += 1
            elif status == KnowledgeJobItem.STATUS_UPDATED:
                counters["updated_count"] += 1
            else:
                counters["skipped_count"] += 1
        except MicrosoftAuthorizationError:
            raise
        except Exception as exc:
            logger.exception("Knowledge page sync failed for %s", external_id)
            _job_item(
                job,
                external_id or f"unknown-{len(seen_ids)}",
                title,
                KnowledgeJobItem.STATUS_FAILED,
                str(exc),
            )
            counters["failed_count"] += 1
        _update_job_heartbeat(job, **counters)

    full_reconcile = bool(job.parameters.get("full_reconcile")) or existing_count == 0
    deleted_count = 0
    if full_reconcile:
        missing = source.documents.exclude(external_id__in=seen_ids).exclude(
            sync_status=KnowledgeDocument.SYNC_SOURCE_DELETED
        )
        deleted_count = missing.count()
        missing.update(
            sync_status=KnowledgeDocument.SYNC_SOURCE_DELETED,
            source_deleted_at=timezone.now(),
        )
        source.last_reconciled_at = timezone.now()
    source.last_sync_at = timezone.now()
    source.status = (
        KnowledgeSource.STATUS_ERROR
        if counters["failed_count"]
        else KnowledgeSource.STATUS_ACTIVE
    )
    source.last_error = (
        f"{counters['failed_count']} 个页面同步失败，请查看任务单项错误。"
        if counters["failed_count"]
        else ""
    )
    source.sync_cursor = {
        "latest_page_modified_at": max(
            (str(page.get("lastModifiedDateTime", "")) for _, page in pages),
            default="",
        ),
        "page_count": len(pages),
    }
    source.save(
        update_fields=[
            "last_sync_at",
            "last_reconciled_at",
            "status",
            "last_error",
            "sync_cursor",
            "updated_at",
        ]
    )
    job.result = {
        "seen_pages": len(seen_ids),
        "source_deleted_marked": deleted_count,
        "full_reconcile": full_reconcile,
    }
    job.save(update_fields=["result", "updated_at"])
    return counters


def _job_document_ids(job, key):
    values = (job.parameters or {}).get(key) or []
    return {
        int(value)
        for value in values
        if str(value).isdigit()
    }


def restore_ai_processing_documents(job):
    document_ids = _job_document_ids(job, "document_ids")
    if not document_ids:
        return 0
    documents = list(
        KnowledgeDocument.objects.filter(
            pk__in=document_ids,
            family=job.family,
            knowledge_status=KnowledgeDocument.KNOWLEDGE_PENDING,
            curation_status=KnowledgeDocument.CURATION_PENDING_AI,
        ).select_related("source", "owner", "current_revision")
    )
    if not documents:
        return 0
    KnowledgeDocument.objects.filter(pk__in=[item.pk for item in documents]).update(
        curation_status=KnowledgeDocument.CURATION_NORMALIZED,
        updated_at=timezone.now(),
    )
    for document in documents:
        document.curation_status = KnowledgeDocument.CURATION_NORMALIZED
        index_document(document)
    return len(documents)


def mark_ai_processing_documents(job):
    document_ids = _job_document_ids(job, "document_ids")
    if not document_ids:
        return 0
    documents = list(
        KnowledgeDocument.objects.filter(
            pk__in=document_ids,
            family=job.family,
            knowledge_status=KnowledgeDocument.KNOWLEDGE_PENDING,
            curation_status__in=[
                KnowledgeDocument.CURATION_INBOX,
                KnowledgeDocument.CURATION_NORMALIZED,
            ],
        ).select_related("source", "owner", "current_revision")
    )
    if not documents:
        return 0
    KnowledgeDocument.objects.filter(pk__in=[item.pk for item in documents]).update(
        curation_status=KnowledgeDocument.CURATION_PENDING_AI,
        updated_at=timezone.now(),
    )
    for document in documents:
        document.curation_status = KnowledgeDocument.CURATION_PENDING_AI
        index_document(document)
    return len(documents)


def generate_source_proposals(job):
    source = KnowledgeSource.objects.select_related("family", "owner").get(pk=job.source_id)
    selected_ids = _job_document_ids(job, "document_ids")
    one_time_ids = _job_document_ids(job, "one_time_document_ids")
    if not selected_ids:
        raise KnowledgeAiError("AI 整理任务未包含成员明确选择的资料，已停止运行。")
    if not source.allow_cloud_ai and not selected_ids.issubset(one_time_ids):
        raise KnowledgeAiError("该来源未授权向云端 AI 发送正文。")
    document_queryset = source.documents.filter(
        current_revision__isnull=False,
        sync_status=KnowledgeDocument.SYNC_AVAILABLE,
        knowledge_status=KnowledgeDocument.KNOWLEDGE_PENDING,
    )
    document_queryset = document_queryset.filter(pk__in=selected_ids)
    documents = list(
        document_queryset.select_related("current_revision", "source", "owner", "family")
    )
    documents_by_id = {document.pk: document for document in documents}
    missing_ids = selected_ids - documents_by_id.keys()
    job.total_count = len(selected_ids)
    job.save(update_fields=["total_count", "updated_at"])
    counters = {"success_count": 0, "updated_count": 0, "skipped_count": 0, "failed_count": 0}
    for document_id in sorted(missing_ids):
        _job_item(
            job,
            f"document-{document_id}",
            f"资料 #{document_id}",
            KnowledgeJobItem.STATUS_FAILED,
            "资料已不在待整理、正文不可用或不属于当前来源。",
        )
        counters["failed_count"] += 1
    for document in documents:
        _check_cancelled(job)
        revision = document.current_revision
        existing = KnowledgeProposal.objects.filter(
            revision=revision,
            prompt_version="knowledge-organize-v1",
        )
        if existing.count() == 3:
            document.curation_status = KnowledgeDocument.CURATION_PENDING_REVIEW
            document.save(update_fields=["curation_status", "updated_at"])
            index_document(document)
            _job_item(
                job,
                f"document-{document.pk}",
                document.title,
                KnowledgeJobItem.STATUS_SKIPPED,
            )
            counters["skipped_count"] += 1
        else:
            try:
                generate_proposals(
                    document,
                    cloud_ai_consent=(
                        "one_time" if document.pk in one_time_ids else "source"
                    ),
                )
                index_document(document)
                _job_item(
                    job,
                    f"document-{document.pk}",
                    document.title,
                    KnowledgeJobItem.STATUS_SUCCESS,
                )
                counters["success_count"] += 1
            except Exception as exc:
                logger.exception("Knowledge proposal generation failed for %s", document.pk)
                if document.curation_status == KnowledgeDocument.CURATION_PENDING_AI:
                    document.curation_status = KnowledgeDocument.CURATION_NORMALIZED
                    document.save(update_fields=["curation_status", "updated_at"])
                    index_document(document)
                _job_item(
                    job,
                    f"document-{document.pk}",
                    document.title,
                    KnowledgeJobItem.STATUS_FAILED,
                    str(exc),
                )
                counters["failed_count"] += 1
        _update_job_heartbeat(job, **counters)
    return counters


def rebuild_search_job(job):
    result = rebuild_family_search(job.family)
    job.total_count = result["notes"] + result["documents"]
    job.result = result
    job.success_count = job.total_count
    job.save(
        update_fields=["total_count", "result", "success_count", "updated_at"]
    )
    return {
        "success_count": job.total_count,
        "updated_count": 0,
        "skipped_count": 0,
        "failed_count": 0,
    }


def recover_stale_jobs():
    now = timezone.now()
    threshold = now - timedelta(minutes=30)
    stale_jobs = list(
        KnowledgeJob.objects.filter(status=KnowledgeJob.STATUS_RUNNING).filter(
            Q(heartbeat_at__lt=threshold)
            | Q(heartbeat_at__isnull=True, started_at__lt=threshold)
        )
    )
    if not stale_jobs:
        return 0
    KnowledgeJob.objects.filter(pk__in=[job.pk for job in stale_jobs]).update(
        status=KnowledgeJob.STATUS_FAILED,
        finished_at=now,
        updated_at=now,
        error_message="任务心跳超过 30 分钟，已标记失败，可安全重试。",
    )
    for job in stale_jobs:
        if job.job_type == KnowledgeJob.TYPE_GENERATE_PROPOSALS:
            restore_ai_processing_documents(job)
    return len(stale_jobs)


def claim_next_job():
    recover_stale_jobs()
    with transaction.atomic():
        job = (
            KnowledgeJob.objects.select_for_update(skip_locked=True)
            .filter(
                status=KnowledgeJob.STATUS_PENDING,
                scheduled_at__lte=timezone.now(),
            )
            .order_by("scheduled_at", "id")
            .first()
        )
        if not job:
            return None
        now = timezone.now()
        job.status = KnowledgeJob.STATUS_RUNNING
        job.started_at = now
        job.heartbeat_at = now
        job.error_message = ""
        job.save(
            update_fields=[
                "status",
                "started_at",
                "heartbeat_at",
                "error_message",
                "updated_at",
            ]
        )
        return job


def process_job(job):
    try:
        if job.job_type == KnowledgeJob.TYPE_SYNC_SOURCE:
            counters = sync_onenote_source(job)
        elif job.job_type == KnowledgeJob.TYPE_GENERATE_PROPOSALS:
            counters = generate_source_proposals(job)
        elif job.job_type == KnowledgeJob.TYPE_REBUILD_SEARCH:
            counters = rebuild_search_job(job)
        elif job.job_type == KnowledgeJob.TYPE_PREVIEW_IMPORT:
            counters = preview_import_batch(job)
        elif job.job_type == KnowledgeJob.TYPE_IMPORT_BATCH:
            counters = import_knowledge_batch(job)
        elif job.job_type == KnowledgeJob.TYPE_ROLLBACK_IMPORT:
            counters = rollback_knowledge_batch(job)
        else:
            raise ValueError(f"不支持的知识任务类型：{job.job_type}")

        job.refresh_from_db()
        for key, value in counters.items():
            setattr(job, key, value)
        job.status = (
            KnowledgeJob.STATUS_PARTIAL
            if counters.get("failed_count")
            else KnowledgeJob.STATUS_SUCCESS
        )
        job.finished_at = timezone.now()
        job.heartbeat_at = job.finished_at
        job.save()
    except (KnowledgeJobCancelled, ImportJobCancelled) as exc:
        _mark_import_batch_failed(job, exc)
        if job.job_type == KnowledgeJob.TYPE_GENERATE_PROPOSALS:
            restore_ai_processing_documents(job)
        job.status = KnowledgeJob.STATUS_CANCELLED
        job.error_message = str(exc)
        job.finished_at = timezone.now()
        job.heartbeat_at = job.finished_at
        job.save(
            update_fields=[
                "status",
                "error_message",
                "finished_at",
                "heartbeat_at",
                "updated_at",
            ]
        )
    except (MicrosoftAuthorizationError, MicrosoftSourceUnavailableError) as exc:
        job.status = KnowledgeJob.STATUS_SOURCE_UNAVAILABLE
        job.error_message = str(exc)
        job.finished_at = timezone.now()
        job.heartbeat_at = job.finished_at
        if job.source_id:
            KnowledgeSource.objects.filter(pk=job.source_id).update(
                status=(
                    KnowledgeSource.STATUS_DISCONNECTED
                    if isinstance(exc, MicrosoftAuthorizationError)
                    else KnowledgeSource.STATUS_ERROR
                ),
                last_error=str(exc)[:2000],
            )
        job.save(
            update_fields=[
                "status",
                "error_message",
                "finished_at",
                "heartbeat_at",
                "updated_at",
            ]
        )
    except KnowledgeAiError as exc:
        restore_ai_processing_documents(job)
        job.status = KnowledgeJob.STATUS_FAILED
        job.error_message = str(exc)[:4000]
        job.finished_at = timezone.now()
        job.heartbeat_at = job.finished_at
        job.save(
            update_fields=[
                "status",
                "error_message",
                "finished_at",
                "heartbeat_at",
                "updated_at",
            ]
        )
    except Exception as exc:
        logger.exception("Knowledge job %s failed", job.pk)
        _mark_import_batch_failed(job, exc)
        if job.job_type == KnowledgeJob.TYPE_GENERATE_PROPOSALS:
            restore_ai_processing_documents(job)
        job.status = KnowledgeJob.STATUS_FAILED
        job.error_message = str(exc)[:4000]
        job.finished_at = timezone.now()
        job.heartbeat_at = job.finished_at
        if job.source_id:
            KnowledgeSource.objects.filter(pk=job.source_id).update(
                status=KnowledgeSource.STATUS_ERROR,
                last_error=str(exc)[:2000],
            )
        job.save(
            update_fields=[
                "status",
                "error_message",
                "finished_at",
                "heartbeat_at",
                "updated_at",
            ]
        )
    return job
