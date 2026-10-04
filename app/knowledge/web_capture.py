"""Archive before AI; source bytes are immutable and all mutations check worker ownership."""
import base64
import hashlib
import html
import json
import mimetypes
import logging
import time
from contextlib import contextmanager
from urllib.parse import urljoin, urlsplit

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import Max
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .ai import KnowledgeAiError, generate_proposals
from .article_extraction import extract_article, snapshot_body
from .content import OneNoteHTMLRewriter, normalize_onenote_html, validate_resource_mime, validate_resource_signature
from .models import KnowledgeAsset, KnowledgeDocument, KnowledgeJob, KnowledgeProposal, KnowledgeRevision, KnowledgeSource, KnowledgeWebCapture
from .search import index_document
from .web_fetch import WebCaptureError, canonical_url, public_addresses, public_request

CONVERTER_VERSION = "web-capture-readability-v2"
MAX_IMAGES = 100
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_BYTES = 50 * 1024 * 1024
logger = logging.getLogger(__name__)


class CaptureStopped(RuntimeError):
    pass


@contextmanager
def saved_file_guard():
    created = []
    try:
        yield created
    except Exception:
        for file in created:
            try:
                file.storage.delete(file.name)
            except OSError:
                logger.warning("Unable to remove orphan web capture file")
        raise


def digest(value):
    return hashlib.sha256(value).hexdigest()


def article_datetime(metadata):
    value = metadata.get("publishedTime") or metadata.get("article:published_time") or metadata.get("datePublished") or ""
    try:
        parsed = parse_datetime(str(value))
        if parsed and timezone.is_naive(parsed):
            # A time without zone cannot be attributed to the NAS timezone.
            return None
        return parsed
    except ValueError:
        return None


def firecrawl_scrape(url):
    key = getattr(settings, "KNOWLEDGE_FIRECRAWL_API_KEY", "")
    if not key:
        raise WebCaptureError("尚未配置 Firecrawl，请由管理员设置 KNOWLEDGE_FIRECRAWL_API_KEY。")
    public_addresses(url)
    body, _ = public_request("https://api.firecrawl.dev/v2/scrape", limit=12 * 1024 * 1024, timeout=90, redirects=0,
        data=json.dumps({"url": url, "formats": ["markdown", "html", "rawHtml"], "onlyMainContent": True, "onlyCleanContent": False, "timeout": 60000, "maxAge": 0, "skipTlsVerification": False, "storeInCache": False, "removeBase64Images": False}).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        payload = json.loads(body)
        data = payload["data"]
        if payload.get("success") is not True or not isinstance(data, dict):
            raise ValueError
        metadata = data.get("metadata") or {}
        if not isinstance(metadata, dict) or int(metadata.get("statusCode") or 200) >= 400:
            raise WebCaptureError("原网页无法访问或需要登录，不能保存完整正文。")
        final_url = canonical_url(metadata.get("sourceURL") or metadata.get("url") or url)
        public_addresses(final_url)
        raw_html = data.get("html")
        if not isinstance(raw_html, str) or not raw_html.strip():
            raise WebCaptureError("抓取服务没有返回可归档正文，请检查链接或改用文件导入。")
        return {"html": raw_html, "rawHtml": str(data.get("rawHtml") or ""), "markdown": str(data.get("markdown") or ""), "metadata": metadata, "url": final_url}
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, WebCaptureError):
            raise
        raise WebCaptureError("网页抓取服务返回格式错误，请稍后重试。") from exc


class WebHTMLRewriter(OneNoteHTMLRewriter):
    def __init__(self, url, resources=None):
        super().__init__(resources or {})
        self.resource_urls = {**self.resource_urls, **{v: v for v in self.resource_urls.values()}}
        self.url = url
        self.images = {}
        self.unresolved_images = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if not self.blocked_depth and tag.lower() == "img":
            raw = values.get("data-fullres-src") or values.get("data-src") or values.get("src") or ""
            absolute = urljoin(self.url, raw)
            if raw:
                self.images.setdefault(absolute, str(values.get("alt") or "")[:300])
            else:
                self.unresolved_images.append(str(values.get("alt") or "正文图片")[:300])
            local = self.resource_urls.get(absolute) or self.resource_urls.get(digest(absolute.encode()))
            if not local:
                self.output.append(f'<p>[图片未保存：{html.escape(str(values.get("alt") or "正文图片"))}]</p>')
                return
            super().handle_starttag(tag, [("src", local), ("alt", values.get("alt", ""))])
            return
        if tag.lower() == "a":
            values["href"] = urljoin(self.url, values.get("href") or "")
        super().handle_starttag(tag, list(values.items()))


def normalize_web_html(snapshot, resources=None):
    parser = WebHTMLRewriter(snapshot["url"], resources)
    parser.feed(snapshot_body(snapshot))
    return normalize_onenote_html("".join(parser.output), {v: v for v in (resources or {}).values()})


def web_revision_content(revision):
    with revision.raw_file.open("rb") as raw:
        snapshot = json.load(raw)
    resources = {a.external_id: reverse("knowledge:asset_download", kwargs={"pk": a.pk}) for a in revision.assets.all()}
    return normalize_web_html(snapshot, resources)


def submit_capture(member, *, url, visibility, note="", organize_with_ai=True):
    url = canonical_url(url)
    with transaction.atomic():
        # Owner lock covers simultaneous first submissions where no capture row exists yet.
        type(member).objects.select_for_update().get(pk=member.pk)
        capture, created = KnowledgeWebCapture.objects.get_or_create(owner=member, url_hash=digest(url.encode()),
            defaults={"family": member.family, "url": url, "visibility": visibility, "note": note, "organize_with_ai": organize_with_ai})
        if created:
            queue_capture(capture)
            capture.refresh_from_db()
        return capture, created


def queue_capture(capture, mode="resume"):
    if mode not in {"resume", "images", "recapture"}:
        raise WebCaptureError("不支持的重试方式。")
    with transaction.atomic():
        capture = KnowledgeWebCapture.objects.select_for_update().get(pk=capture.pk)
        if capture.last_job_id and capture.last_job.status in KnowledgeJob.ACTIVE_STATUSES:
            return capture.last_job, False
        job = KnowledgeJob.objects.create(family=capture.family, requested_by=capture.owner, job_type=KnowledgeJob.TYPE_CAPTURE_WEB,
            parameters={"capture_id": capture.pk, "mode": mode}, total_count=1)
        capture.last_job = job
        capture.stage, capture.error_message = "queued", ""
        capture.save(update_fields=["last_job", "stage", "error_message", "updated_at"])
        return job, True


def checkpoint(job, stage=None):
    capture = KnowledgeWebCapture.objects.select_for_update().get(pk=job.parameters["capture_id"])
    current = KnowledgeJob.objects.select_for_update().get(pk=job.pk)
    if capture.last_job_id != job.pk or current.status != KnowledgeJob.STATUS_RUNNING or current.started_at != job.started_at:
        raise CaptureStopped()
    if stage:
        capture.stage = stage
        capture.save(update_fields=["stage", "updated_at"])
    KnowledgeJob.objects.filter(pk=job.pk).update(heartbeat_at=timezone.now())
    return capture


def save_snapshot(job, snapshot):
    # Store both the immutable acquisition and the exact derived body used for images/AI.
    snapshot = {**snapshot, "article": extract_article(snapshot)}
    raw_bytes = json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode()
    safe, text = normalize_web_html(snapshot)
    if not text.strip():
        raise WebCaptureError("抓取结果没有可阅读正文，不能作为有效收藏。")
    with saved_file_guard() as created_files, transaction.atomic():
        capture = checkpoint(job, "images")
        host = urlsplit(capture.url).hostname
        source, _ = KnowledgeSource.objects.get_or_create(family=capture.family, key=f"web:{capture.owner_id}:{capture.visibility}:{host}",
            defaults={"owner": capture.owner, "kind": KnowledgeSource.KIND_WEB_CAPTURE, "name": host, "visibility": capture.visibility})
        document = capture.document
        if document is None:
            document = KnowledgeDocument.objects.create(family=capture.family, owner=capture.owner, source=source,
                external_id=capture.url_hash, title=str(snapshot["metadata"].get("title") or capture.url)[:500], source_url=capture.url,
                author=str(snapshot["metadata"].get("author") or "")[:300], visibility=capture.visibility,
                knowledge_status=KnowledgeDocument.KNOWLEDGE_PENDING, curation_status=KnowledgeDocument.CURATION_NORMALIZED)
            capture.document = document
            capture.save(update_fields=["document", "updated_at"])
        revision, created = KnowledgeRevision.objects.get_or_create(document=document, content_hash=digest(raw_bytes),
            defaults={"revision_number": (document.revisions.aggregate(n=Max("revision_number"))["n"] or 0) + 1,
                "raw_file": "", "converter_version": CONVERTER_VERSION, "normalized_html": safe, "plain_text": text, "normalized_hash": digest(text.encode())})
        if created:
            revision.raw_file.save("page.json", ContentFile(raw_bytes), save=False)
            created_files.append(revision.raw_file)
            revision.save(update_fields=["raw_file"])
        if document.current_revision_id != revision.pk:
            KnowledgeProposal.objects.filter(document=document, status=KnowledgeProposal.STATUS_PENDING).update(status=KnowledgeProposal.STATUS_STALE)
            document.curation_status = KnowledgeDocument.CURATION_NORMALIZED
        document.current_revision = revision
        document.title = str(snapshot["metadata"].get("title") or capture.url)[:500]
        document.author = str(snapshot["metadata"].get("author") or "")[:300]
        document.content_created_at = article_datetime(snapshot["metadata"])
        document.content_modified_at = document.content_created_at
        document.source_url = snapshot["url"]
        document.save(update_fields=["current_revision", "curation_status", "title", "author", "source_url", "content_created_at", "content_modified_at", "updated_at"])
        index_document(document)
        return capture


def archive_images(job, capture):
    revision = capture.document.current_revision
    with revision.raw_file.open("rb") as raw:
        snapshot = json.load(raw)
    parser = WebHTMLRewriter(snapshot["url"])
    parser.feed(snapshot_body(snapshot))
    failures = [{"url": "", "alt": alt, "error": "抓取结果未提供可下载图片地址。"} for alt in parser.unresolved_images]
    stored = set(revision.assets.values_list("external_id", flat=True))
    total = sum(revision.assets.values_list("byte_size", flat=True))
    deadline = time.monotonic() + 180
    for number, (url, alt) in enumerate(parser.images.items()):
        with transaction.atomic():
            checkpoint(job, "images")
        if digest(url.encode()) in stored:
            continue
        try:
            if number >= MAX_IMAGES or total >= MAX_TOTAL_BYTES or time.monotonic() >= deadline:
                raise WebCaptureError("已达到单页图片数量、总大小或时间限制。")
            limit = min(MAX_IMAGE_BYTES, MAX_TOTAL_BYTES - total)
            if url.startswith("data:"):
                header, encoded = url.split(",", 1)
                if header not in {"data:image/png;base64", "data:image/jpeg;base64", "data:image/gif;base64", "data:image/webp;base64"} or len(encoded) > (limit + 2) // 3 * 4:
                    raise WebCaptureError("内嵌图片类型或大小不受支持。")
                body = base64.b64decode(encoded, validate=True)
                mime = header[5:].split(";", 1)[0]
                if len(body) > limit:
                    raise WebCaptureError("内嵌图片超过大小限制。")
            else:
                if len(url) > 1000:
                    raise WebCaptureError("图片链接超过长度限制。")
                body, mime = public_request(url, limit=limit, timeout=min(20, deadline-time.monotonic()))
            mime = validate_resource_mime(mime, True, body)
            validate_resource_signature(body, mime)
            with saved_file_guard() as created_files, transaction.atomic():
                checkpoint(job)
                asset = KnowledgeAsset.objects.create(revision=revision, external_id=digest(url.encode()), source_path="" if url.startswith("data:") else url,
                    original_name=alt, mime_type=mime, byte_size=len(body), content_hash=digest(body), is_image=True, file="")
                asset.file.save(f"image-{asset.external_id[:24]}{mimetypes.guess_extension(mime) or '.bin'}", ContentFile(body), save=False)
                created_files.append(asset.file)
                asset.save(update_fields=["file"])
            total += len(body)
        except ValueError as exc:
            failures.append({"url": url[:1000], "alt": alt, "error": str(exc)[:300]})
    with transaction.atomic():
        capture = checkpoint(job)
        revision.normalized_html, revision.plain_text = web_revision_content(revision)
        revision.normalized_hash = digest(revision.plain_text.encode())
        revision.save(update_fields=["normalized_html", "plain_text", "normalized_hash"])
        capture.image_failures = failures
        capture.save(update_fields=["image_failures", "updated_at"])
        index_document(capture.document)
    return capture


def process_capture_job(job):
    try:
        with transaction.atomic():
            capture = checkpoint(job, "capture")
        mode = job.parameters.get("mode", "resume")
        previous_revision_id = capture.document.current_revision_id if capture.document_id else None
        if not capture.document_id or mode == "recapture":
            capture = save_snapshot(job, firecrawl_scrape(capture.url))
        capture = archive_images(job, capture)
        document = capture.document
        revision_changed = previous_revision_id != document.current_revision_id
        needs_ai = not document.proposal_runs.filter(revision=document.current_revision).exists() or (mode == "recapture" and revision_changed)
        if capture.organize_with_ai and mode != "images" and needs_ai:
            with transaction.atomic():
                checkpoint(job, "ai")
            if len(document.current_revision.plain_text) > 80000:
                raise KnowledgeAiError("正文已保存；超过 AI 完整分析长度限制，请人工整理或拆分后导入。")
            generate_proposals(document, cloud_ai_consent="one_time", requested_by=capture.owner, before_save=lambda: checkpoint(job))
            index_document(document)
        with document.current_revision.raw_file.open("rb") as raw:
            article = json.load(raw).get("article") or {}
        extraction_report = {key: article[key] for key in ("method", "version", "text_length", "image_count") if key in article}
        with transaction.atomic():
            capture = checkpoint(job, "done")
            capture.error_message = ""
            capture.save(update_fields=["error_message", "updated_at"])
            KnowledgeJob.objects.filter(pk=job.pk).update(status=KnowledgeJob.STATUS_PARTIAL if capture.image_failures else KnowledgeJob.STATUS_SUCCESS,
                success_count=1, failed_count=0, result={"document_id": document.pk, "missing_images": len(capture.image_failures),
                    "article_extraction": extraction_report}, finished_at=timezone.now(), heartbeat_at=timezone.now())
    except CaptureStopped:
        KnowledgeJob.objects.filter(pk=job.pk, status=KnowledgeJob.STATUS_CANCEL_REQUESTED, started_at=job.started_at).update(status=KnowledgeJob.STATUS_CANCELLED, finished_at=timezone.now())
    except Exception as exc:
        message = str(exc) if isinstance(exc, (WebCaptureError, KnowledgeAiError)) else "保存遇到内部错误，请重试；已归档正文不会丢失。"
        with transaction.atomic():
            try:
                capture = checkpoint(job)
            except CaptureStopped:
                return
            capture.stage, capture.error_message = "failed", message[:2000]
            capture.save(update_fields=["stage", "error_message", "updated_at"])
            KnowledgeJob.objects.filter(pk=job.pk).update(status=KnowledgeJob.STATUS_FAILED, failed_count=1, error_message=message[:2000], finished_at=timezone.now(), heartbeat_at=timezone.now())
    job.refresh_from_db()
