from django.db.models import CharField, Q
from django.db.models.functions import Cast

from family_core.models import FamilyMember
from family_core.permissions import current_member

from .models import (
    KnowledgeArtifact,
    KnowledgeDocument,
    KnowledgeSearchEntry,
    KnowledgeSource,
    KnowledgeVisibility,
    SourceConnection,
)


def accessible_documents(member):
    return (
        KnowledgeDocument.objects.filter(family=member.family)
        .filter(
            Q(owner=member)
            | Q(
                visibility=KnowledgeVisibility.FAMILY,
                source__visibility=KnowledgeVisibility.FAMILY,
            )
        )
        .select_related("source", "owner", "current_revision")
    )


def accessible_search_entries(member):
    from notes.models import InvestmentNote

    notes = InvestmentNote.objects.filter(
        family=member.family, include_in_knowledge=True,
    ).filter(Q(member=member) | Q(visibility=KnowledgeVisibility.FAMILY))
    return (
        KnowledgeSearchEntry.objects.filter(family=member.family)
        .filter(
            Q(item_kind=KnowledgeSearchEntry.KIND_DOCUMENT,
              document__in=accessible_documents(member))
            | Q(item_kind=KnowledgeSearchEntry.KIND_ARTIFACT,
                artifact__in=accessible_artifacts(member))
            | Q(item_kind=KnowledgeSearchEntry.KIND_INVESTMENT_NOTE,
                object_id__in=notes.annotate(text_id=Cast("pk", CharField())).values("text_id"))
        )
        .select_related("owner", "document", "document__source", "artifact")
    )


def accessible_artifacts(member):
    return (
        KnowledgeArtifact.objects.filter(family=member.family)
        .filter(Q(owner=member) | Q(visibility=KnowledgeVisibility.FAMILY))
        .select_related("owner", "current_version", "confirmed_by")
    )


def visible_sources(member):
    queryset = KnowledgeSource.objects.filter(family=member.family).select_related(
        "owner",
        "connection",
    )
    if member.role == FamilyMember.ROLE_ADMIN:
        return queryset
    return queryset.filter(
        Q(owner=member)
        | Q(
            visibility=KnowledgeVisibility.FAMILY,
            kind=KnowledgeSource.KIND_INTERNAL_NOTES,
        )
    )


def visible_connections(member):
    queryset = SourceConnection.objects.filter(family=member.family).select_related("member")
    if member.role == FamilyMember.ROLE_ADMIN:
        return queryset
    return queryset.filter(member=member)


def can_manage_source(member, source):
    return (
        source.family_id == member.family_id
        and (
            source.owner_id == member.id
            or member.role == FamilyMember.ROLE_ADMIN
        )
    )


def can_change_source_settings(member, source):
    return (
        source.family_id == member.family_id
        and (
            source.owner_id == member.id
            or (
                source.owner_id is None
                and member.role == FamilyMember.ROLE_ADMIN
            )
        )
    )


def can_organize_document(member, document):
    return (
        document.family_id == member.family_id
        and (
            document.owner_id == member.id
            or member.role == FamilyMember.ROLE_ADMIN
        )
    )


def can_manage_artifact(member, artifact):
    return (
        artifact.family_id == member.family_id
        and (
            artifact.owner_id == member.id
            or member.role == FamilyMember.ROLE_ADMIN
        )
    )
