"""Explicit document lifecycle; immutable identity prevents sync resurrection.

Source/capture locks are shared with enqueueing. Active jobs must finish before
destructive actions. File deletion happens only after the DB commit and has a
durable retry receipt. Neither external originals nor shared import packages,
reading originals or backups are deleted here.
"""
from pathlib import Path, PurePosixPath

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from family_core.models import FamilyMember
from .models import (KnowledgeAsset, KnowledgeArtifactEvidence, KnowledgeCurationRevision,
    KnowledgeDocument, KnowledgeFileCleanup, KnowledgeImportItem, KnowledgeJob,
    KnowledgeLifecycleEvent, KnowledgeProposal, KnowledgeProposalRun, KnowledgeRevision,
    KnowledgeSource, KnowledgeWebCapture)
from .permissions import accessible_documents, can_organize_document
from .search import index_document


def _locked_document(pk, member):
    document = accessible_documents(member, include_trashed=True, include_purged=True).filter(pk=pk).first()
    if document is None or member.role == FamilyMember.ROLE_VIEWER or not can_organize_document(member, document):
        raise ValidationError("你没有管理这篇资料的权限。")
    # The same lock ordering as source enqueue / web capture enqueue.
    KnowledgeSource.objects.select_for_update().get(pk=document.source_id)
    list(KnowledgeWebCapture.objects.select_for_update().filter(document_id=pk))
    document = KnowledgeDocument.objects.select_for_update().get(pk=pk)
    if not accessible_documents(member, include_trashed=True, include_purged=True).filter(pk=pk).exists():
        raise ValidationError("资料的访问权限已变化，请刷新页面。")
    active = KnowledgeJob.objects.filter(family=member.family, status__in=KnowledgeJob.ACTIVE_STATUSES)
    if active.filter(Q(source_id=document.source_id) | Q(web_captures__document_id=pk)).exists():
        raise ValidationError("该资料或来源仍有排队、运行或取消中的任务。请等待任务结束后再管理资料。")
    return document


def _event(document, member, action, numbers=()):
    return KnowledgeLifecycleEvent.objects.create(document=document, actor=member, action=action, revision_numbers=list(numbers))


@transaction.atomic
def change_document(pk, member, action):
    document = _locked_document(pk, member)
    if document.purged_at:
        raise ValidationError("这篇资料的内容已彻底删除，不能恢复或再次整理。")
    if action == "trash":
        if document.trashed_at:
            return document
        document.trashed_at = timezone.now()
    elif action == "restore":
        if not document.trashed_at:
            raise ValidationError("这篇资料不在回收站。")
        document.trashed_at = None
    elif action == "unfeature":
        if document.trashed_at or document.library_tier != document.LIBRARY_KNOWLEDGE:
            raise ValidationError("这篇资料不在精选知识中。")
        document.library_tier = document.LIBRARY_ARCHIVE
        document.knowledge_status = document.KNOWLEDGE_INCLUDED
        document.curation_status = document.CURATION_NORMALIZED
        # Pending recommendations must not promote it again without a new choice.
        document.proposals.filter(status=KnowledgeProposal.STATUS_PENDING).update(status=KnowledgeProposal.STATUS_REJECTED)
    else:
        raise ValidationError("不支持的资料管理操作。")
    document.save()
    index_document(document)
    _event(document, member, action)
    return document


def validate_file(document, name):
    """Only individual files belonging to this document; never a directory/glob."""
    prefix = f"families/{document.family_id}/sources/{document.source_id}/documents/{document.pk}/revisions/"
    parts = PurePosixPath(name).parts
    if not name.startswith(prefix) or ".." in parts or "\\" in name or len(parts) != 10 or not parts[7].isdigit() or parts[8] not in {"raw", "assets"}:
        raise ValidationError("文件路径不属于这篇资料，未执行清理。")
    storage = KnowledgeRevision._meta.get_field("raw_file").storage
    root = Path(storage.location)
    path = Path(storage.path(name))
    if not path.resolve().is_relative_to(root.resolve()) or path.resolve() == root.resolve() or any(p.is_symlink() for p in [path, *path.parents]):
        raise ValidationError("文件路径超出资料目录或含链接，未执行清理。")
    return storage


def _files_for(document, revisions):
    names = [r.raw_file.name for r in revisions if r.raw_file]
    names += list(KnowledgeAsset.objects.filter(revision__in=revisions).exclude(file="").values_list("file", flat=True))
    result = []
    for name in sorted(set(names)):
        # Reading publications share the original file; retain it in its source module.
        if name.startswith("reading/"):
            continue
        validate_file(document, name)
        result.append(name)
    return result


def _clear_revisions(document, revisions):
    from ai_analysis.models import AiAnalysisRequest, AiAnalysisResult
    ids = [r.pk for r in revisions]
    runs = KnowledgeProposalRun.objects.filter(revision_id__in=ids)
    request_ids = list(runs.exclude(analysis_request=None).values_list("analysis_request_id", flat=True))
    # Include failed/unapplied requests which never created a proposal run.
    request_ids += list(AiAnalysisRequest.objects.filter(family_id=document.family_id, module="knowledge", scope__document_id=document.pk, scope__revision_id__in=ids).values_list("pk", flat=True))
    AiAnalysisRequest.objects.filter(pk__in=request_ids).update(prompt="", sanitized_input={}, scope={}, error_message="")
    AiAnalysisResult.objects.filter(request_id__in=request_ids).update(result_text="", result_json={})
    KnowledgeProposal.objects.filter(revision_id__in=ids).delete()
    # A confirmed human summary is a separate record, preserved when only old captures are cleaned.
    KnowledgeCurationRevision.objects.filter(proposal_run__in=runs).update(proposal_run=None)
    runs.delete()
    KnowledgeAsset.objects.filter(revision_id__in=ids).delete()
    KnowledgeRevision.objects.filter(pk__in=ids).update(raw_file="", normalized_html="", plain_text="", normalized_hash="", purged_at=timezone.now())
    KnowledgeImportItem.objects.filter(Q(revision_id__in=ids) | Q(previous_revision_id__in=ids)).update(previous_state={})


@transaction.atomic
def request_cleanup(pk, member, *, whole_document=False):
    document = _locked_document(pk, member)
    if whole_document:
        if document.purged_at or not document.trashed_at:
            raise ValidationError("请先把资料移入回收站，再确认彻底删除。")
        if document.artifact_evidence_links.exists() or KnowledgeArtifactEvidence.objects.filter(revision__document=document).exists():
            raise ValidationError("专题成果仍引用这篇资料；为保留证据链，不能彻底删除。")
        revisions = list(document.revisions.filter(purged_at__isnull=True))
    else:
        if document.trashed_at or document.purged_at:
            raise ValidationError("请先恢复资料，再清理旧版本。")
        revisions = list(document.revisions.filter(purged_at__isnull=True).exclude(pk=document.current_revision_id).filter(artifact_evidence_links__isnull=True))
        if not revisions:
            raise ValidationError("没有可清理的旧版本；当前版本和被专题引用的版本会保留。")
    files = _files_for(document, revisions)
    task = KnowledgeFileCleanup.objects.create(document=document, files=files)
    numbers = [r.revision_number for r in revisions]
    _clear_revisions(document, revisions)
    if whole_document:
        document.purged_at = timezone.now()
        document.current_revision = None
        document.title = "已彻底删除的资料"
        document.author = document.section_name = document.source_url = document.confirmed_summary = document.category = ""
        document.tags, document.hierarchy = [], {}
        document.content_created_at = document.content_modified_at = None
        document.save()
        document.curation_revisions.all().delete()
        document.import_items.update(title="", author="", source_url="", details={}, previous_state={})
        # Keep the opaque URL hash so re-submission cannot silently resurrect it.
        KnowledgeWebCapture.objects.filter(document=document).update(url="", note="", image_failures=[], error_message="")
        for capture in KnowledgeWebCapture.objects.filter(document=document).select_related("last_job"):
            KnowledgeJob.objects.filter(family=document.family, job_type=KnowledgeJob.TYPE_CAPTURE_WEB, parameters__capture_id=capture.pk).update(result={}, error_message="")
        from .models import KnowledgeJobItem
        KnowledgeJobItem.objects.filter(job__source=document.source, external_id__in=[document.external_id, f"document-{document.pk}"]).update(title="已删除的资料", error_message="", details={})
        index_document(document)
    _event(document, member, "purge" if whole_document else "versions", numbers)
    return task


def process_cleanup(task_id):
    """Idempotent deletion after commit; failures stay visible and retryable."""
    with transaction.atomic():
        task = KnowledgeFileCleanup.objects.select_for_update().select_related("document").get(pk=task_id)
        if task.status == "success":
            return True
        try:
            targets = [(name, validate_file(task.document, name)) for name in task.files]
            for name, storage in targets:
                from .models import KnowledgeArtifactVersion, KnowledgeImportBatch
                if (KnowledgeRevision.objects.filter(raw_file=name).exists() or KnowledgeAsset.objects.filter(file=name).exists()
                        or KnowledgeArtifactVersion.objects.filter(Q(original_file=name) | Q(rendered_file=name)).exists()
                        or KnowledgeImportBatch.objects.filter(package_file=name).exists()):
                    raise ValidationError("文件仍被其他内容引用。")
            for name, storage in targets:
                storage.delete(name)
        except (OSError, ValidationError):
            task.status, task.error = "failed", "文件清理未完成，请重试；已确认的内容删除无法撤销。"
            task.save(update_fields=["status", "error", "updated_at"])
            return False
        task.status, task.error, task.files, task.finished_at = "success", "", [], timezone.now()
        task.save(update_fields=["status", "error", "files", "finished_at", "updated_at"])
        return True
