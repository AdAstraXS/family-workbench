"""Permanent deletion with a durable, retryable file-cleanup receipt."""
import re
from pathlib import Path

from django.core.exceptions import ValidationError
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from .models import (Book, BookFile, BookLifecycleEvent, BookPurgeTask, Annotation,
    AnnotationComment, ReadingPlanItem, ReadingArtifact, ReadingArtifactVersion,
    ReadingArchive, ReadingAiJob)
from .storage import storage


def directory_files(name):
    if not re.fullmatch(r"(?:artifacts/)?[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", name):
        raise ValidationError("文件目录无效，未执行永久删除。")
    root = Path(storage().location)
    target = root / name
    if root.is_symlink() or any(p.is_symlink() for p in [target, *target.parents]):
        raise ValidationError("文件目录含链接，未执行永久删除。")
    if not target.resolve().is_relative_to(root.resolve()) or target.resolve() == root.resolve():
        raise ValidationError("文件目录超出图书存储范围。")
    entries = list(target.rglob("*")) if target.exists() else []
    if any(p.is_symlink() for p in entries):
        raise ValidationError("文件目录含链接，未执行永久删除。")
    return target, entries


def request_purge(book, member):
    from knowledge.models import KnowledgeRevision
    from ai_analysis.models import AiAnalysisRequest, AiAnalysisResult
    with transaction.atomic():
        book = Book.objects.select_for_update().filter(pk=book.pk, owner=member, family=member.family).first()
        if book is None or member.role == "viewer" or not book.deleted_at:
            raise ValidationError("只有上传人可永久删除回收站中的图书。")
        # Import workers claim the file row; holding it closes the claim/delete race.
        file = BookFile.objects.select_for_update().get(book=book)
        if file.status == "processing" or book.ai_jobs.filter(status="running").exists():
            raise ValidationError("图书仍在解析或生成 AI 内容，请等待任务结束后再永久删除。")
        artifacts = list(ReadingArtifact.objects.filter(book=book))
        names = [str(book.pk), *[f"artifacts/{a.pk}" for a in artifacts]]
        directories = []
        for name in names:
            directory_files(name)  # Validate every target before removing any records.
            prefix = f"reading/{name}/"
            preserve = [str(r.raw_file)[len("reading/"):] for r in
                KnowledgeRevision.objects.filter(raw_file__startswith=prefix)]
            directories.append({"name": name, "preserve": preserve})
        for path in [file.original_path, file.normalized_path]:
            if path and not path.startswith(f"{book.pk}/"):
                raise ValidationError("图书文件路径异常，未执行永久删除。")
        versions = ReadingArtifactVersion.objects.filter(artifact__book=book)
        for version in versions:
            if version.original_path and not version.original_path.startswith(f"artifacts/{version.artifact_id}/"):
                raise ValidationError("阅读成果路径异常，未执行永久删除。")
        # Archived knowledge keeps its own immutable raw file and body. Remove the obsolete reading link.
        for archive in ReadingArchive.objects.filter(version__in=versions).select_related("version"):
            version = archive.version
            prefix = (f'<p>阅读成果发布版本 v{version.number} · <a href="'
                f'{reverse("reading:artifact_version", args=[version.artifact_id, version.number])}">'
                '查看导图、依据及原文件</a></p>')
            for revision in KnowledgeRevision.objects.filter(document_id=archive.document_id):
                if revision.normalized_html.startswith(prefix):
                    revision.normalized_html = '<p>原图书及阅读成果已永久删除；此归档版本独立保留。</p>' + revision.normalized_html[len(prefix):]
                    revision.save(update_fields=["normalized_html"])
        task = BookPurgeTask.objects.create(book_id=book.pk, owner=member, title=book.title, directories=directories)
        request_ids = list(book.ai_jobs.exclude(analysis_request=None).values_list("analysis_request_id", flat=True))
        ReadingAiJob.objects.filter(book=book).delete()
        # Preserve usage/cost audit, remove book excerpts and generated private content from AI audit.
        AiAnalysisRequest.objects.filter(pk__in=request_ids).update(prompt="", sanitized_input={}, scope={}, error_message="")
        AiAnalysisResult.objects.filter(request_id__in=request_ids).update(result_text="", result_json={})
        ReadingArchive.objects.filter(version__in=versions).delete()
        ReadingArtifact.objects.filter(book=book).update(current_version=None)
        versions.delete()
        ReadingArtifact.objects.filter(book=book).delete()
        AnnotationComment.objects.filter(annotation__book=book).delete()
        Annotation.objects.filter(book=book).delete()
        ReadingPlanItem.objects.filter(book=book).delete()
        BookLifecycleEvent.objects.filter(book=book).delete()
        book.delete()  # Cascades file/import runs and personal positions.
        return task


def process_purge(task_id):
    """DB deletion is committed first; cleanup can safely retry after any filesystem error."""
    with transaction.atomic():
        task = BookPurgeTask.objects.select_for_update().get(pk=task_id)
        if task.status == "success":
            return True
        try:
            targets = []
            for entry in task.directories:
                target, files = directory_files(entry["name"])
                preserve = set(entry["preserve"])
                # Check all paths before doing any unlink; never follow symlinks.
                targets.append((target, files, preserve))
            for target, files, preserve in targets:
                for path in files:
                    if path.is_file() and path.relative_to(Path(storage().location)).as_posix() not in preserve:
                        path.unlink(missing_ok=True)
                for path in sorted([p for p in files if p.is_dir()], key=lambda p: len(p.parts), reverse=True):
                    if not any(path.iterdir()):
                        path.rmdir()
                if target.exists() and not any(target.iterdir()):
                    target.rmdir()
        except (OSError, ValidationError):
            task.status = "failed"
            task.error = "文件清理未完成，请重试；已确认的永久删除无法撤销。"
            task.save(update_fields=["status", "error", "updated_at"])
            return False
        task.status = "success"
        task.error = ""
        task.directories = []
        task.finished_at = timezone.now()
        task.save(update_fields=["status", "error", "directories", "finished_at", "updated_at"])
        return True
