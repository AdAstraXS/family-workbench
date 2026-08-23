from django.db.models import Q

from family_core.models import FamilyMember

from .models import (
    KnowledgeArtifact,
    KnowledgeDocument,
    KnowledgeSearchEntry,
    KnowledgeSource,
    KnowledgeVisibility,
    SourceConnection,
)


def current_member(request):
    member = getattr(request, "family_member", None)
    if member is not None and member.is_active:
        return member
    try:
        member = request.user.family_member
    except FamilyMember.DoesNotExist:
        return None
    return member if member.is_active else None


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
    return (
        KnowledgeSearchEntry.objects.filter(family=member.family)
        .filter(Q(owner=member) | Q(visibility=KnowledgeVisibility.FAMILY))
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
