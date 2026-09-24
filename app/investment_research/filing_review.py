"""新财报复核：只把成员确认的判断与固定原文引用写入档案。"""

from django.db import transaction
from django.utils import timezone

from .citations import locate_quote
from .models import (
    DOC_TYPE_10K, DOC_TYPE_10Q, SOURCE_SEC, OfficialResearchContentVersion,
    OfficialResearchDocument, ResearchDossier, ResearchFilingReview,
    ResearchThesisRevision,
)
from .services import DossierNotFound, ResearchValidationError, _require_writer

ASSESSMENT_CHOICES = {"supports", "weakens", "unanswered"}


class FilingReviewConflict(ResearchValidationError):
    """页面打开后判断版本发生变化；不得把复核挂到旧判断上。"""


def reviewable_filings(dossier):
    """仅把第一版判断之后正式发布的 SEC 10-Q/10-K 列为新财报。"""
    first = dossier.revisions.order_by("revision_number", "pk").first()
    if first is None:
        return None, []
    after_date = timezone.localdate(first.created_at)
    documents = list(OfficialResearchDocument.objects.filter(
        security=dossier.security, source=SOURCE_SEC,
        document_type__in=(DOC_TYPE_10Q, DOC_TYPE_10K),
        published_at__gt=after_date,
    ).prefetch_related("content_versions").order_by("-published_at", "-pk"))
    latest_by_document = {}
    for review in ResearchFilingReview.objects.filter(dossier=dossier).order_by("-created_at", "-pk"):
        latest_by_document.setdefault(review.document_id, review)
    items = []
    for document in documents:
        latest = latest_by_document.get(document.pk)
        needs_revision = bool(
            latest and latest.action == ResearchFilingReview.ACTION_REVISE and
            latest.thesis_revision_id == dossier.current_revision_id
        )
        items.append({
            "document": document,
            "version": next(iter(document.content_versions.all()), None),
            "review": latest,
            "needs_revision": needs_revision,
            "pending": latest is None,
        })
    return after_date, items


def save_filing_review(*, actor, dossier_id, document_id, version_id,
                       expected_revision_id, assessments, outcome, action,
                       summary, follow_up="", quote=""):
    _require_writer(actor)
    if not isinstance(outcome, str) or outcome not in dict(ResearchFilingReview.OUTCOME_CHOICES):
        raise ResearchValidationError("请选择总体复核结果。")
    if not isinstance(action, str) or action not in dict(ResearchFilingReview.ACTION_CHOICES):
        raise ResearchValidationError("请选择是否需要修订判断。")
    summary = str(summary or "").strip()
    follow_up = str(follow_up or "").strip()
    if not 1 <= len(summary) <= 3000 or len(follow_up) > 2000:
        raise ResearchValidationError("复核结论须为 1–3000 字，后续待查不超过 2000 字。")
    with transaction.atomic():
        dossier = ResearchDossier.objects.select_for_update().filter(
            pk=dossier_id, owner=actor, family=actor.family,
        ).first()
        if dossier is None:
            raise DossierNotFound("研究档案不存在或不属于你。")
        revision = ResearchThesisRevision.objects.filter(
            pk=expected_revision_id, dossier=dossier,
        ).first()
        if revision is None or dossier.current_revision_id != revision.pk:
            raise FilingReviewConflict("判断已更新，请重新打开财报复核页。")
        first = dossier.revisions.order_by("revision_number", "pk").first()
        if first is None:
            raise FilingReviewConflict("档案尚未保存第一版判断。")
        document = OfficialResearchDocument.objects.filter(
            pk=document_id, security=dossier.security, source=SOURCE_SEC,
            document_type__in=(DOC_TYPE_10Q, DOC_TYPE_10K),
            published_at__gt=timezone.localdate(first.created_at),
        ).first()
        if document is None:
            raise DossierNotFound("这份财报不属于当前档案的新财报清单。")
        version = OfficialResearchContentVersion.objects.filter(
            pk=version_id, document=document,
        ).first()
        if version is None:
            raise DossierNotFound("请先保存这份 SEC 财报正文。")
        expected = [("pillar", text) for text in revision.pillars]
        expected += [("question", text) for text in revision.questions]
        if not isinstance(assessments, list) or len(assessments) != len(expected):
            raise ResearchValidationError("请逐项核对当前判断的假设与问题。")
        cleaned = []
        for item, (kind, text) in zip(assessments, expected):
            if not isinstance(item, dict) or item.get("kind") != kind or item.get("text") != text:
                raise ResearchValidationError("核查条目与当前判断不一致。")
            status = item.get("status")
            note = item.get("note")
            if status not in ASSESSMENT_CHOICES or not isinstance(note, str) or len(note.strip()) > 500:
                raise ResearchValidationError("请选择每项核查结果；备注不超过 500 字。")
            cleaned.append({"kind": kind, "text": text, "status": status,
                            "note": note.strip()})
        citation = {}
        if quote:
            try:
                start, end, digest = locate_quote(version, quote)
            except ValueError as exc:
                raise ResearchValidationError(str(exc)) from exc
            citation = {"start": start, "end": end, "hash": digest}
        elif outcome != ResearchFilingReview.OUTCOME_UNCLEAR:
            raise ResearchValidationError("有明确结论时，请复制一段这份财报中的原文作为证据。")
        return ResearchFilingReview.objects.create(
            dossier=dossier, document=document, content_version=version,
            thesis_revision=revision, assessments=cleaned,
            outcome=outcome, action=action, summary=summary,
            follow_up=follow_up, citation=citation, created_by=actor,
        )
