import hashlib
import logging
import re
import uuid
import zipfile
from datetime import timedelta
from pathlib import Path

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone
from family_core.models import FamilyMember

from .importer import ImportFailure, VERSION, normalize_epub, normalize_txt
from .models import Book, BookFile, ReadingImportRun, ReadingPosition
from .storage import max_upload_bytes, storage

logger = logging.getLogger(__name__)


class DuplicateBook(Exception):
    def __init__(self, book):
        self.book = book


def upload_book(member, cleaned):
    upload = cleaned["file"]
    digest, size = hashlib.sha256(), 0
    for chunk in upload.chunks():
        size += len(chunk)
        if size > max_upload_bytes():
            raise ValidationError("文件超过上传上限。")
        digest.update(chunk)
    if not size:
        raise ValidationError("不能上传空文件。")
    upload.seek(0)
    suffix = Path(upload.name).suffix.lower().lstrip(".")
    if suffix not in {"epub", "pdf", "txt", "mobi"}:
        raise ValidationError("文件格式不受支持。")
    saved = None
    try:
        with transaction.atomic():
            # Serialize duplicate submissions for this member, not other members' private books.
            FamilyMember.objects.select_for_update().get(pk=member.pk)
            existing = Book.objects.filter(owner=member, file__sha256=digest.hexdigest()).first()
            if existing:
                raise DuplicateBook(existing)
            book = Book.objects.create(family=member.family, owner=member, title=cleaned["title"],
                                       author=cleaned.get("author", ""), visibility=cleaned["visibility"])
            saved = storage().save(f"{book.pk}/original.{suffix}", upload)
            BookFile.objects.create(book=book, original_path=saved, original_name=Path(upload.name).name[:255],
                                    sha256=digest.hexdigest(), size=size, format=suffix)
        return book
    except Exception:
        if saved:
            storage().delete(saved)
        raise


def process_file(file_id):
    token = uuid.uuid4()
    now = timezone.now()
    with transaction.atomic():
        claimed = BookFile.objects.filter(pk=file_id, status="queued").update(
            status="processing", lease=token, processing_started_at=now, error="", updated_at=now)
        if not claimed:
            return False
        run = ReadingImportRun.objects.create(file_id=file_id, token=token)
    file = BookFile.objects.select_related("book").get(pk=file_id)
    target_name = f"{file.book_id}/normalized-{token}.epub"
    target = Path(storage().path(target_name))
    source = Path(storage().path(file.original_path))
    result = {"status": "ready", "error": "", "normalizer_version": VERSION}
    try:
        # Confirm immutable original before deriving anything.
        with source.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != file.sha256:
                raise ImportFailure("原件校验不一致，请检查文件存储；未继续处理。")
        if file.format == "epub":
            resources, sections = normalize_epub(source, target)
            result.update(normalized_path=target_name, resources=resources, sections=sections, text_status="available")
        elif file.format == "txt":
            resources, sections = normalize_txt(source, target, file.book.title)
            result.update(normalized_path=target_name, resources=resources, sections=sections, text_status="available")
        elif file.format == "pdf":
            with source.open("rb") as stream:
                if not stream.read(1024).lstrip().startswith(b"%PDF-"):
                    raise ImportFailure("文件不是可识别的 PDF。")
                stream.seek(max(0, file.size - 4096))
                if b"%%EOF" not in stream.read():
                    raise ImportFailure("PDF 文件可能未完整上传，缺少结束标记。")
            from .text import pdf_text
            try:
                inspection = pdf_text(file)
            except ValidationError as exc:
                raise ImportFailure(exc.messages[0]) from exc
            result.update(text_status="unknown", page_count=inspection["page_count"])
        else:
            result.update(status="unsupported", error="MOBI 原件已保存，本阶段尚未接入安全正文转换与在线阅读。")
    except (ImportFailure, zipfile.BadZipFile, KeyError, OSError, ValueError) as exc:
        message = str(exc) if isinstance(exc, ImportFailure) else "文件结构损坏或存储不可用，请检查原件后重试。"
        result = {"status": "failed", "error": message}
    except Exception:
        logger.exception("Reading import failed for file id %s", file_id)
        result = {"status": "failed", "error": "处理遇到异常，请重试或联系管理员查看任务记录。"}
    with transaction.atomic():
        won = BookFile.objects.filter(pk=file_id, status="processing", lease=token).update(
            **result, lease=None, processing_started_at=None, updated_at=timezone.now())
        ReadingImportRun.objects.filter(pk=run.pk).update(
            status=result["status"] if won else "superseded", message=result.get("error", ""), finished_at=timezone.now())
    if (not won or result["status"] != "ready") and target.exists():
        target.unlink()  # Only this attempt's generated derivative, never original content.
    return result["status"] == "ready" and bool(won)


def retry_file(book):
    now = timezone.now()
    with transaction.atomic():
        file = BookFile.objects.select_for_update().get(book=book)
        if file.status == "processing" and file.processing_started_at and file.processing_started_at < now - timedelta(minutes=15):
            ReadingImportRun.objects.filter(file=file, token=file.lease, status="processing").update(
                status="expired", finished_at=now, message="处理超过 15 分钟，由上传者重新排队。")
        elif file.status != "failed":
            raise ValidationError("只可重试失败任务，或已超过 15 分钟的处理任务。")
        file.status, file.lease, file.error = "queued", None, ""
        file.processing_started_at = None
        file.save(update_fields=["status", "lease", "error", "processing_started_at", "updated_at"])


def position_payload(position):
    if position is None:
        return {"revision": 0, "location": {}, "progress": 0, "completed_at": None}
    return {"revision": position.revision, "location": position.location, "progress": position.progress,
            "completed_at": position.completed_at.isoformat() if position.completed_at else None}


def validate_location(file, data):
    if data.get("file_hash") != file.sha256 or data.get("normalizer_version") != file.normalizer_version:
        raise ValidationError("阅读文件版本不一致，请重新打开图书。")
    revision, progress, location = data.get("revision"), data.get("progress"), data.get("location")
    if type(revision) is not int or revision < 0 or type(progress) is not int or not 0 <= progress <= 10000:
        raise ValidationError("阅读进度或版本号无效。")
    if not isinstance(location, dict):
        raise ValidationError("阅读位置无效。")
    if file.format == "pdf":
        page = location.get("page")
        if type(page) is not int or not 1 <= page <= (file.page_count or 100000):
            raise ValidationError("PDF 页码无效。")
        return {"page": page}
    cfi = location.get("cfi")
    if not isinstance(cfi, str) or len(cfi) > 3000 or not re.fullmatch(r"epubcfi\([^\r\n<>]+\)", cfi):
        raise ValidationError("章节位置无效。")
    return {"cfi": cfi}


def save_position(book, member, data):
    """Compare-and-swap: stale clients get 409, never overwrite silently."""
    location = validate_location(book.file, data)
    now = timezone.now()
    values = dict(location=location, progress=data["progress"], file_hash=book.file.sha256, updated_at=now)
    if data["revision"] == 0:
        try:
            with transaction.atomic():
                obj = ReadingPosition.objects.create(book=book, member=member, **values)
            return obj, True
        except IntegrityError:
            return ReadingPosition.objects.get(book=book, member=member), False
    updated = ReadingPosition.objects.filter(book=book, member=member, revision=data["revision"]).update(
        **values, revision=data["revision"] + 1)
    return ReadingPosition.objects.filter(book=book, member=member).first(), bool(updated)
