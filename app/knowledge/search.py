from datetime import datetime, time

from django.db import transaction
from django.utils import timezone

from notes.models import InvestmentNote

from .models import (
    KnowledgeDocument,
    KnowledgeSearchEntry,
    KnowledgeSource,
    KnowledgeVisibility,
)


def _searchable_text(*values):
    return "\n".join(str(value or "") for value in values).casefold()


def _tags_text(tags):
    return "\n".join(str(tag) for tag in (tags or []))


def _note_content_time(note):
    value = datetime.combine(note.note_date, time.min)
    return timezone.make_aware(value, timezone.get_current_timezone())


def ensure_internal_notes_source(family):
    source, _ = KnowledgeSource.objects.get_or_create(
        family=family,
        key="internal:investment-notes",
        defaults={
            "kind": KnowledgeSource.KIND_INTERNAL_NOTES,
            "name": "随手记",
            "visibility": KnowledgeVisibility.FAMILY,
            "status": KnowledgeSource.STATUS_ACTIVE,
        },
    )
    return source


def index_investment_note(note):
    if not note.include_in_knowledge:
        remove_investment_note_index(note)
        return None
    ensure_internal_notes_source(note.family)
    tags = note.tags or []
    is_curated = note.knowledge_state == InvestmentNote.KNOWLEDGE_CURATED
    defaults = {
        "owner": note.member,
        "visibility": note.visibility,
        "title": note.title,
        "body": note.content,
        "summary": note.ai_summary,
        "source_kind": KnowledgeSource.KIND_INTERNAL_NOTES,
        "source_name": "随手记",
        "author_name": note.member.display_name,
        "category": note.note_type.name,
        "tags": tags,
        "tags_text": _tags_text(tags),
        "searchable_text": _searchable_text(
            note.title,
            note.content,
            note.ai_summary,
            note.note_type.name,
            _tags_text(tags),
            note.member.display_name,
        ),
        "curation_status": (
            KnowledgeDocument.CURATION_CONFIRMED
            if is_curated
            else KnowledgeDocument.CURATION_INBOX
        ),
        "knowledge_status": (
            KnowledgeDocument.KNOWLEDGE_INCLUDED
            if is_curated
            else KnowledgeDocument.KNOWLEDGE_PENDING
        ),
        "content_time": _note_content_time(note),
        "document": None,
    }
    entry, _ = KnowledgeSearchEntry.objects.update_or_create(
        family=note.family,
        item_kind=KnowledgeSearchEntry.KIND_INVESTMENT_NOTE,
        object_id=str(note.pk),
        defaults=defaults,
    )
    return entry


def remove_investment_note_index(note):
    KnowledgeSearchEntry.objects.filter(
        family=note.family,
        item_kind=KnowledgeSearchEntry.KIND_INVESTMENT_NOTE,
        object_id=str(note.pk),
    ).delete()


def index_document(document):
    if not document.owner_id or not document.current_revision_id:
        KnowledgeSearchEntry.objects.filter(
            family=document.family,
            item_kind=KnowledgeSearchEntry.KIND_DOCUMENT,
            object_id=str(document.pk),
        ).delete()
        return None
    tags = document.tags or []
    revision = document.current_revision
    hierarchy = document.hierarchy or {}
    section_group = hierarchy.get("section_group", "")
    defaults = {
        "owner": document.owner,
        "visibility": document.visibility,
        "title": document.title,
        "body": revision.plain_text,
        "summary": document.confirmed_summary,
        "source_kind": document.source.kind,
        "source_name": document.source.name,
        "author_name": document.author or (
            document.owner.display_name if document.owner else ""
        ),
        "category": document.category,
        "tags": tags,
        "tags_text": _tags_text(tags),
        "searchable_text": _searchable_text(
            document.title,
            revision.plain_text,
            document.confirmed_summary,
            document.category,
            _tags_text(tags),
            document.author,
            document.source.name,
            section_group,
            document.section_name,
        ),
        "curation_status": document.curation_status,
        "knowledge_status": document.knowledge_status,
        "content_time": document.content_modified_at or document.updated_at,
        "document": document,
    }
    entry, _ = KnowledgeSearchEntry.objects.update_or_create(
        family=document.family,
        item_kind=KnowledgeSearchEntry.KIND_DOCUMENT,
        object_id=str(document.pk),
        defaults=defaults,
    )
    return entry


@transaction.atomic
def rebuild_family_search(family):
    KnowledgeSearchEntry.objects.filter(family=family).delete()
    notes_count = 0
    for note in (
        InvestmentNote.objects.filter(family=family, include_in_knowledge=True)
        .select_related("member", "note_type")
        .iterator()
    ):
        index_investment_note(note)
        notes_count += 1
    document_count = 0
    for document in (
        KnowledgeDocument.objects.filter(family=family, current_revision__isnull=False)
        .select_related("source", "owner", "current_revision")
        .iterator()
    ):
        index_document(document)
        document_count += 1
    return {"notes": notes_count, "documents": document_count}
