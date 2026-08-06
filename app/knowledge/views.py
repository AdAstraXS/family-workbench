import re
from collections import Counter
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Case, Count, IntegerField, Q, When
from django.http import FileResponse, Http404, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_POST

from family_core.models import FamilyMember

from .forms import (
    BulkProposalPreviewForm,
    DocumentOrganizeForm,
    NotebookSelectionForm,
    ProposalReviewForm,
)
from .microsoft import (
    MicrosoftAuthorizationError,
    MicrosoftConfigurationError,
    MicrosoftGraphClient,
    MicrosoftKnowledgeError,
    finish_authorization_flow,
    microsoft_is_configured,
    safe_notebook_cache,
    start_authorization_flow,
)
from .models import (
    KnowledgeAsset,
    KnowledgeDocument,
    KnowledgeJob,
    KnowledgeProposal,
    KnowledgeSearchEntry,
    KnowledgeSource,
    KnowledgeVisibility,
    SourceConnection,
)
from .permissions import (
    accessible_documents,
    accessible_search_entries,
    can_change_source_settings,
    can_manage_source,
    can_organize_document,
    current_member,
    visible_connections,
    visible_sources,
)
from .search import index_document
from .services import queue_knowledge_job


def _membership_required_response(request):
    return render(request, "knowledge/membership_required.html", status=403)


def _can_write(member):
    return member.role != FamilyMember.ROLE_VIEWER


def _knowledge_status_for_route(route):
    return {
        KnowledgeSource.ROUTE_ORGANIZE: KnowledgeDocument.KNOWLEDGE_PENDING,
        KnowledgeSource.ROUTE_ARCHIVE: KnowledgeDocument.KNOWLEDGE_ARCHIVED,
    }.get(route, KnowledgeDocument.KNOWLEDGE_INCLUDED)


def _source_sections(source):
    sections = {}
    for document in source.documents.order_by("section_name", "id").only(
        "section_name",
        "hierarchy",
    ):
        hierarchy = document.hierarchy or {}
        section_id = str(hierarchy.get("section_id") or document.section_name)
        if not section_id:
            continue
        item = sections.setdefault(
            section_id,
            {
                "id": section_id,
                "name": document.section_name or "未分区",
                "group": str(hierarchy.get("section_group") or ""),
                "count": 0,
            },
        )
        item["count"] += 1
    for item in sections.values():
        item["route"] = source.route_for_section(item["id"])
    return sorted(
        sections.values(),
        key=lambda item: (item["group"], item["name"], item["id"]),
    )


def _redirect_uri(request):
    return settings.KNOWLEDGE_MICROSOFT_REDIRECT_URI or request.build_absolute_uri(
        reverse("knowledge:microsoft_callback")
    )


def _entry_target(entry):
    if entry.item_kind == KnowledgeSearchEntry.KIND_DOCUMENT and entry.document_id:
        return reverse("knowledge:document_detail", kwargs={"pk": entry.document_id})
    if entry.item_kind == KnowledgeSearchEntry.KIND_INVESTMENT_NOTE:
        return reverse("notes:detail", kwargs={"pk": int(entry.object_id)})
    return reverse("knowledge:library")


def _hit_snippet(entry, query):
    values = [entry.summary, entry.body]
    if not query:
        return (entry.summary or entry.body)[:220]
    normalized = query.casefold()
    for value in values:
        value = value or ""
        position = value.casefold().find(normalized)
        if position >= 0:
            start = max(0, position - 80)
            end = min(len(value), position + len(query) + 140)
            prefix = "…" if start else ""
            suffix = "…" if end < len(value) else ""
            return f"{prefix}{value[start:end]}{suffix}"
    return (entry.summary or entry.body)[:220]


def _manageable_proposals(member):
    queryset = KnowledgeProposal.objects.filter(
        document__in=accessible_documents(member),
        revision=models_current_revision(),
    ).select_related(
        "document",
        "document__source",
        "document__owner",
        "revision",
        "confirmed_by",
    )
    if member.role != FamilyMember.ROLE_ADMIN:
        queryset = queryset.filter(document__owner=member)
    return queryset


def models_current_revision():
    from django.db.models import F

    return F("document__current_revision")


def _decorate_entries(entries, query=""):
    document_ids = [
        entry.document_id
        for entry in entries
        if entry.item_kind == KnowledgeSearchEntry.KIND_DOCUMENT
        and entry.document_id
    ]
    image_document_ids = set(
        KnowledgeAsset.objects.filter(
            revision__document_id__in=document_ids,
            is_image=True,
        ).values_list("revision__document_id", flat=True)
    )
    for entry in entries:
        entry.target_url = _entry_target(entry)
        entry.hit_snippet = _hit_snippet(entry, query)
        entry.has_original = entry.item_kind == KnowledgeSearchEntry.KIND_DOCUMENT
        entry.has_images = entry.document_id in image_document_ids
    return entries


def _knowledge_stats(member):
    entries = accessible_search_entries(member).filter(owner=member)
    documents = accessible_documents(member).filter(owner=member)
    today = timezone.localdate()
    return {
        "total": entries.filter(
            knowledge_status=KnowledgeDocument.KNOWLEDGE_INCLUDED,
        ).count(),
        "today_new": entries.filter(
            knowledge_status=KnowledgeDocument.KNOWLEDGE_INCLUDED,
            created_at__date=today,
        ).count(),
        "inbox": entries.filter(
            item_kind=KnowledgeSearchEntry.KIND_DOCUMENT,
            knowledge_status=KnowledgeDocument.KNOWLEDGE_PENDING,
        ).count(),
        "pending_review": documents.filter(
            curation_status=KnowledgeDocument.CURATION_PENDING_REVIEW
        ).count(),
        "source_errors": visible_sources(member).filter(
            Q(owner=member) | Q(owner__isnull=True),
            status__in=[
                KnowledgeSource.STATUS_ERROR,
                KnowledgeSource.STATUS_DISCONNECTED,
            ]
        ).count(),
    }


def _build_source_directory(entries):
    sources = {}
    document_rows = entries.filter(
        item_kind=KnowledgeSearchEntry.KIND_DOCUMENT,
        document__isnull=False,
    ).values(
        "document__source_id",
        "source_name",
        "source_kind",
        "document__section_name",
        "document__hierarchy__section_group",
    ).annotate(total=Count("id"))
    for row in document_rows.iterator():
        source_id = str(row["document__source_id"])
        source = sources.setdefault(
            source_id,
            {
                "id": source_id,
                "name": row["source_name"],
                "kind": row["source_kind"],
                "count": 0,
                "sections": {},
            },
        )
        row_count = row["total"]
        source["count"] += row_count
        section_name = (row["document__section_name"] or "").strip()
        section_group = str(
            row["document__hierarchy__section_group"] or ""
        ).strip()
        section_key = (section_group, section_name)
        section = source["sections"].setdefault(
            section_key,
            {
                "name": section_name or "未分区",
                "value": section_name,
                "group": section_group,
                "count": 0,
            },
        )
        section["count"] += row_count

    note_count = entries.filter(
        item_kind=KnowledgeSearchEntry.KIND_INVESTMENT_NOTE
    ).count()
    if note_count:
        sources["notes"] = {
            "id": "notes",
            "name": "随手记",
            "kind": KnowledgeSource.KIND_INTERNAL_NOTES,
            "count": note_count,
            "sections": {},
        }

    directory = []
    for source in sources.values():
        source["sections"] = sorted(
            source["sections"].values(),
            key=lambda item: (item["group"], item["name"]),
        )
        directory.append(source)
    return sorted(directory, key=lambda item: (item["name"], item["id"]))


@login_required
def index(request):
    member = current_member(request)
    if member is None:
        return _membership_required_response(request)

    entries = accessible_search_entries(member).filter(
        owner=member,
        knowledge_status=KnowledgeDocument.KNOWLEDGE_INCLUDED,
    )
    recent_entries = _decorate_entries(list(entries[:6]))
    today_entries = _decorate_entries(
        list(entries.filter(created_at__date=timezone.localdate())[:6])
    )
    recent_confirmed = _decorate_entries(list(entries[:4]))
    source_issues = list(
        visible_sources(member)
        .filter(
            Q(owner=member) | Q(owner__isnull=True),
            status__in=[
                KnowledgeSource.STATUS_ERROR,
                KnowledgeSource.STATUS_DISCONNECTED,
            ]
        )
        .order_by("-updated_at")[:4]
    )
    return render(
        request,
        "knowledge/home.html",
        {
            "stats": _knowledge_stats(member),
            "today_entries": today_entries,
            "recent_entries": recent_entries,
            "recent_confirmed": recent_confirmed,
            "source_issues": source_issues,
        },
    )


def _library_response(
    request,
    member,
    *,
    page_title="知识库",
    page_description="默认查看已经入库的知识，并默认显示当前登录成员；可切换“全部资料”检查待整理或仅同步归档内容。",
):
    entries = accessible_search_entries(member)
    collection = request.GET.get("collection", "library").strip()
    if collection == "all":
        pass
    else:
        collection = "library"
        entries = entries.filter(
            knowledge_status=KnowledgeDocument.KNOWLEDGE_INCLUDED,
        )
    query = request.GET.get("q", "").strip()
    scope = request.GET.get("scope", "all")
    source_kind = request.GET.get("source", "").strip()
    owner_id = request.GET.get("member")
    owner_id = owner_id.strip() if owner_id is not None else str(member.pk)
    curation_status = request.GET.get("status", "").strip()
    tag = request.GET.get("tag", "").strip()
    author = request.GET.get("person", "").strip()
    content_type = request.GET.get("kind", "all").strip()
    directory_mode = request.GET.get("directory", "category").strip()
    category = request.GET.get("category", "").strip()
    source_id = request.GET.get("source_id", "").strip()
    section = request.GET.get("section", "").strip()
    section_group = request.GET.get("section_group", "").strip()
    quick_filter = request.GET.get("quick", "").strip()
    date_from = request.GET.get("date_from", "").strip()
    date_to = request.GET.get("date_to", "").strip()

    if directory_mode not in {"category", "source"}:
        directory_mode = "category"

    if scope == "personal":
        entries = entries.filter(owner=member)
    elif scope == "family":
        entries = entries.filter(visibility=KnowledgeVisibility.FAMILY)
    else:
        scope = "all"
    if source_kind:
        entries = entries.filter(source_kind=source_kind)
    family_members = FamilyMember.objects.filter(
        family=member.family,
        is_active=True,
    ).order_by("display_order", "id")
    valid_member_ids = {str(value) for value in family_members.values_list("pk", flat=True)}
    if owner_id == "all":
        pass
    elif owner_id in valid_member_ids:
        entries = entries.filter(owner_id=int(owner_id))
    else:
        owner_id = str(member.pk)
        entries = entries.filter(owner=member)
    if content_type == "notes":
        entries = entries.filter(item_kind=KnowledgeSearchEntry.KIND_INVESTMENT_NOTE)
    elif content_type == "external":
        entries = entries.filter(item_kind=KnowledgeSearchEntry.KIND_DOCUMENT)
    else:
        content_type = "all"

    directory_entries = entries
    directory_total = directory_entries.count()
    category_directory = list(
        directory_entries.exclude(category="")
        .values("category")
        .annotate(total=Count("id"))
        .order_by("category")
    )
    uncategorized_count = directory_entries.filter(category="").count()
    source_directory = _build_source_directory(directory_entries)

    if category:
        entries = entries.filter(category=category)
    if source_id == "notes":
        entries = entries.filter(
            item_kind=KnowledgeSearchEntry.KIND_INVESTMENT_NOTE
        )
    elif source_id.isdigit():
        entries = entries.filter(
            item_kind=KnowledgeSearchEntry.KIND_DOCUMENT,
            document__source_id=int(source_id),
        )
        if section:
            entries = entries.filter(document__section_name=section)
        if section_group:
            entries = entries.filter(
                document__hierarchy__section_group=section_group
            )
    else:
        source_id = ""
        section = ""
        section_group = ""

    if quick_filter == "recent":
        entries = entries.filter(content_time__gte=timezone.now() - timedelta(days=30))
    elif quick_filter == "uncategorized":
        entries = entries.filter(category="")
    else:
        quick_filter = ""
    if curation_status:
        entries = entries.filter(curation_status=curation_status)
    if tag:
        entries = entries.filter(tags__contains=[tag])
    if author:
        entries = entries.filter(author_name__icontains=author)
    parsed_from = parse_date(date_from)
    parsed_to = parse_date(date_to)
    if parsed_from:
        entries = entries.filter(content_time__date__gte=parsed_from)
    else:
        date_from = ""
    if parsed_to:
        entries = entries.filter(content_time__date__lte=parsed_to)
    else:
        date_to = ""
    if query:
        normalized = query.casefold()
        entries = entries.filter(searchable_text__icontains=normalized).annotate(
            search_rank=Case(
                When(title__iexact=query, then=0),
                When(title__istartswith=query, then=1),
                When(title__icontains=query, then=2),
                default=3,
                output_field=IntegerField(),
            )
        ).order_by("search_rank", "-content_time", "-updated_at")

    page_obj = Paginator(entries, 20).get_page(request.GET.get("page"))
    _decorate_entries(page_obj.object_list, query)
    pagination_params = request.GET.copy()
    pagination_params.pop("page", None)

    source_choices = (
        directory_entries
        .values("source_kind", "source_name")
        .annotate(total=Count("id"))
        .order_by("source_name")
    )
    selected_source_name = ""
    for source in source_directory:
        if source["id"] == source_id:
            selected_source_name = source["name"]
            break
    if category:
        directory_title = category
    elif section:
        directory_title = " / ".join(
            value
            for value in [selected_source_name, section_group, section]
            if value
        )
    elif selected_source_name:
        directory_title = selected_source_name
    elif quick_filter == "recent":
        directory_title = "最近 30 天"
    elif quick_filter == "uncategorized":
        directory_title = "未分类"
    else:
        directory_title = "全部资料" if collection == "all" else "全部知识"
    return render(
        request,
        "knowledge/index.html",
        {
            "page_obj": page_obj,
            "query": query,
            "scope": scope,
            "selected_source": source_kind,
            "selected_member": owner_id,
            "selected_status": curation_status,
            "selected_collection": collection,
            "selected_tag": tag,
            "selected_author": author,
            "selected_kind": content_type,
            "directory_mode": directory_mode,
            "category_directory": category_directory,
            "source_directory": source_directory,
            "directory_total": directory_total,
            "uncategorized_count": uncategorized_count,
            "selected_category": category,
            "selected_source_id": source_id,
            "selected_source_name": selected_source_name,
            "selected_section": section,
            "selected_section_group": section_group,
            "quick_filter": quick_filter,
            "directory_title": directory_title,
            "date_from": date_from,
            "date_to": date_to,
            "source_choices": source_choices,
            "family_members": family_members,
            "curation_choices": KnowledgeDocument.CURATION_CHOICES,
            "stats": _knowledge_stats(member),
            "page_title": page_title,
            "page_description": page_description,
            "current_member": member,
            "pagination_query": pagination_params.urlencode(),
        },
    )


@login_required
def library(request):
    member = current_member(request)
    if member is None:
        return _membership_required_response(request)
    return _library_response(request, member)


@login_required
def personal_library(request):
    member = current_member(request)
    if member is None:
        return _membership_required_response(request)
    return redirect("knowledge:library")


@login_required
def family_library(request):
    member = current_member(request)
    if member is None:
        return _membership_required_response(request)
    return redirect(reverse("knowledge:library") + "?member=all")


@login_required
def inbox(request):
    member = current_member(request)
    if member is None:
        return _membership_required_response(request)
    entries = accessible_search_entries(member).filter(
        item_kind=KnowledgeSearchEntry.KIND_DOCUMENT,
        knowledge_status=KnowledgeDocument.KNOWLEDGE_PENDING,
    )
    status_counts = {
        item["curation_status"]: item["total"]
        for item in entries.order_by()
        .values("curation_status")
        .annotate(total=Count("id"))
    }
    selected_status = request.GET.get("status", "").strip()
    allowed_statuses = {
        KnowledgeDocument.CURATION_INBOX,
        KnowledgeDocument.CURATION_NORMALIZED,
        KnowledgeDocument.CURATION_PENDING_AI,
        KnowledgeDocument.CURATION_PENDING_REVIEW,
    }
    if selected_status in allowed_statuses:
        entries = entries.filter(curation_status=selected_status)
    else:
        selected_status = ""
    page_obj = Paginator(entries, 20).get_page(request.GET.get("page"))
    _decorate_entries(page_obj.object_list)
    return render(
        request,
        "knowledge/inbox.html",
        {
            "page_obj": page_obj,
            "selected_collection": "all",
            "status_counts": status_counts,
            "selected_status": selected_status,
            "curation_filters": [
                {
                    "value": value,
                    "label": label,
                    "total": status_counts.get(value, 0),
                }
                for value, label in KnowledgeDocument.CURATION_CHOICES
                if value in allowed_statuses
            ],
        },
    )


@login_required
def topics(request):
    member = current_member(request)
    if member is None:
        return _membership_required_response(request)
    entries = accessible_search_entries(member).filter(
        knowledge_status=KnowledgeDocument.KNOWLEDGE_INCLUDED,
    )
    tag_counts = Counter()
    for tags in entries.values_list("tags", flat=True)[:5000]:
        tag_counts.update(str(tag).strip() for tag in (tags or []) if str(tag).strip())
    category_counts = list(
        entries.exclude(category="")
        .order_by()
        .values("category")
        .annotate(total=Count("id"))
        .order_by("-total", "category")[:30]
    )
    return render(
        request,
        "knowledge/topics.html",
        {
            "tags": tag_counts.most_common(60),
            "categories": category_counts,
            "entry_count": entries.count(),
        },
    )


@login_required
def people(request):
    member = current_member(request)
    if member is None:
        return _membership_required_response(request)
    entries = accessible_search_entries(member).filter(
        knowledge_status=KnowledgeDocument.KNOWLEDGE_INCLUDED,
    )
    authors = list(
        entries.exclude(author_name="")
        .order_by()
        .values("author_name")
        .annotate(total=Count("id"))
        .order_by("-total", "author_name")[:40]
    )
    selected_person = request.GET.get("person", "").strip()
    timeline = entries.none()
    if selected_person:
        timeline = entries.filter(author_name=selected_person)[:50]
    timeline = _decorate_entries(list(timeline))
    return render(
        request,
        "knowledge/people.html",
        {
            "authors": authors,
            "selected_person": selected_person,
            "timeline": timeline,
        },
    )
@login_required
def architecture(request):
    member = current_member(request)
    if member is None:
        return _membership_required_response(request)
    return render(request, "knowledge/architecture.html")


@login_required
def document_detail(request, pk):
    member = current_member(request)
    if member is None:
        return _membership_required_response(request)
    document = get_object_or_404(accessible_documents(member), pk=pk)
    proposals = document.proposals.filter(
        revision=document.current_revision,
    ).select_related("confirmed_by")
    response = render(
        request,
        "knowledge/document_detail.html",
        {
            "document": document,
            "revision": document.current_revision,
            "proposals": proposals,
            "can_organize": _can_write(member)
            and can_organize_document(member, document),
        },
    )
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self'; style-src 'self'; "
        "script-src 'none'; object-src 'none'; frame-src 'none'; base-uri 'none'"
    )
    return response


@login_required
def document_organize(request, pk):
    member = current_member(request)
    if member is None:
        return _membership_required_response(request)
    document = get_object_or_404(accessible_documents(member), pk=pk)
    if not _can_write(member) or not can_organize_document(member, document):
        return HttpResponseForbidden("只有资料所有者或家庭管理员可以整理这项知识。")
    if request.method == "POST":
        form = DocumentOrganizeForm(request.POST, instance=document)
        if form.is_valid():
            with transaction.atomic():
                document = form.save()
                if document.knowledge_status == KnowledgeDocument.KNOWLEDGE_INCLUDED:
                    document.curation_status = KnowledgeDocument.CURATION_CONFIRMED
                    document.save(
                        update_fields=["curation_status", "updated_at"]
                    )
                KnowledgeProposal.objects.filter(
                    document=document,
                    revision=document.current_revision,
                    status=KnowledgeProposal.STATUS_PENDING,
                ).update(
                    status=KnowledgeProposal.STATUS_REJECTED,
                    confirmed_by=member,
                    confirmed_at=timezone.now(),
                    human_value={"reason": "manual_organization"},
                )
                index_document(document)
            messages.success(request, "正式摘要、分类和标签已保存。")
            return redirect("knowledge:document_detail", pk=document.pk)
    else:
        form = DocumentOrganizeForm(instance=document)
    return render(
        request,
        "knowledge/document_organize.html",
        {"document": document, "form": form},
    )


def _open_protected_file(field_file):
    try:
        return field_file.storage.open(field_file.name, "rb")
    except (FileNotFoundError, OSError) as exc:
        raise Http404("受保护文件不存在或已损坏。") from exc


@login_required
def asset_download(request, pk):
    member = current_member(request)
    if member is None:
        raise Http404
    asset = get_object_or_404(
        KnowledgeAsset.objects.select_related(
            "revision__document__source",
            "revision__document__owner",
        ),
        pk=pk,
    )
    if not accessible_documents(member).filter(pk=asset.revision.document_id).exists():
        raise Http404
    inline = asset.mime_type in {
        "image/gif",
        "image/jpeg",
        "image/png",
        "image/webp",
    }
    response = FileResponse(
        _open_protected_file(asset.file),
        as_attachment=not inline,
        filename=Path(asset.original_name or "resource.bin").name,
        content_type=asset.mime_type if inline else "application/octet-stream",
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "private, no-store"
    return response


@login_required
def revision_raw_download(request, pk):
    member = current_member(request)
    if member is None:
        raise Http404
    document = get_object_or_404(accessible_documents(member), revisions__pk=pk)
    revision = get_object_or_404(document.revisions, pk=pk)
    response = FileResponse(
        _open_protected_file(revision.raw_file),
        as_attachment=True,
        filename=f"knowledge-{document.pk}-v{revision.revision_number}.html",
        content_type="application/octet-stream",
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "private, no-store"
    return response


@login_required
def sources(request):
    member = current_member(request)
    if member is None:
        return _membership_required_response(request)
    connections = visible_connections(member)
    own_connection = connections.filter(
        member=member,
        provider=SourceConnection.PROVIDER_MICROSOFT,
    ).first()
    return render(
        request,
        "knowledge/sources.html",
        {
            "connections": connections,
            "own_connection": own_connection,
            "sources": visible_sources(member),
            "microsoft_configured": microsoft_is_configured(),
            "can_write": _can_write(member),
        },
    )


@login_required
@require_POST
def microsoft_start(request):
    member = current_member(request)
    if member is None:
        return _membership_required_response(request)
    if not _can_write(member):
        return HttpResponseForbidden("只读成员不能绑定外部账户。")
    try:
        flow = start_authorization_flow(_redirect_uri(request))
    except MicrosoftKnowledgeError as exc:
        messages.error(request, str(exc))
        return redirect("knowledge:sources")
    request.session["knowledge_microsoft_flow"] = flow
    request.session["knowledge_microsoft_member_id"] = member.pk
    return redirect(flow["auth_uri"])


@login_required
def microsoft_callback(request):
    member = current_member(request)
    if member is None:
        return _membership_required_response(request)
    flow = request.session.pop("knowledge_microsoft_flow", None)
    member_id = request.session.pop("knowledge_microsoft_member_id", None)
    if not flow or member_id != member.pk:
        messages.error(request, "Microsoft 授权会话不存在或已过期，请重新绑定。")
        return redirect("knowledge:sources")
    try:
        result, serialized_cache = finish_authorization_flow(flow, request.GET.dict())
        claims = result.get("id_token_claims") or {}
        connection, _ = SourceConnection.objects.get_or_create(
            family=member.family,
            member=member,
            provider=SourceConnection.PROVIDER_MICROSOFT,
        )
        connection.set_token_cache(serialized_cache)
        connection.external_account_id = str(
            claims.get("oid") or claims.get("sub") or ""
        )[:255]
        connection.account_display_name = str(claims.get("name") or "")[:200]
        connection.account_email = str(
            claims.get("preferred_username") or claims.get("email") or ""
        )[:320]
        connection.granted_scopes = str(result.get("scope") or "").split()
        connection.status = SourceConnection.STATUS_ACTIVE
        connection.last_error = ""
        connection.save()

        client = MicrosoftGraphClient(connection)
        profile = client.profile()
        connection.external_account_id = str(
            profile.get("id") or connection.external_account_id
        )[:255]
        connection.account_display_name = str(
            profile.get("displayName") or connection.account_display_name
        )[:200]
        connection.account_email = str(
            profile.get("mail")
            or profile.get("userPrincipalName")
            or connection.account_email
        )[:320]
        connection.available_notebooks = safe_notebook_cache(client.notebooks())
        connection.last_success_at = timezone.now()
        connection.save()
        messages.success(request, "Microsoft 账户已绑定，请选择用于第一轮验证的笔记本。")
    except (MicrosoftKnowledgeError, MicrosoftConfigurationError) as exc:
        messages.error(request, str(exc))
    return redirect("knowledge:sources")


@login_required
@require_POST
def notebooks_refresh(request):
    member = current_member(request)
    if member is None:
        return _membership_required_response(request)
    connection = get_object_or_404(
        SourceConnection,
        family=member.family,
        member=member,
        provider=SourceConnection.PROVIDER_MICROSOFT,
    )
    try:
        client = MicrosoftGraphClient(connection)
        connection.available_notebooks = safe_notebook_cache(client.notebooks())
        connection.status = SourceConnection.STATUS_ACTIVE
        connection.last_error = ""
        connection.last_success_at = timezone.now()
        connection.save()
        messages.success(request, "笔记本列表已刷新。")
    except MicrosoftKnowledgeError as exc:
        messages.error(request, str(exc))
    return redirect("knowledge:sources")


@login_required
@require_POST
def notebook_select(request):
    member = current_member(request)
    if member is None:
        return _membership_required_response(request)
    if not _can_write(member):
        return HttpResponseForbidden("只读成员不能选择同步来源。")
    connection = get_object_or_404(
        SourceConnection,
        family=member.family,
        member=member,
        provider=SourceConnection.PROVIDER_MICROSOFT,
        status=SourceConnection.STATUS_ACTIVE,
    )
    form = NotebookSelectionForm(
        request.POST,
        notebooks=connection.available_notebooks,
    )
    if not form.is_valid():
        for errors in form.errors.values():
            for error in errors:
                messages.error(request, error)
        return redirect("knowledge:sources")
    notebook = form.selected_notebook
    notebook_id = form.cleaned_data["notebook_id"]
    source, created = KnowledgeSource.objects.update_or_create(
        family=member.family,
        key=f"onenote:{member.pk}:{notebook_id}",
        defaults={
            "owner": member,
            "connection": connection,
            "kind": KnowledgeSource.KIND_ONENOTE,
            "name": notebook["displayName"],
            "external_id": notebook_id,
            "source_url": notebook.get("webUrl", ""),
            "visibility": form.cleaned_data["visibility"],
            "allow_cloud_ai": form.cleaned_data["allow_cloud_ai"],
            "status": KnowledgeSource.STATUS_ACTIVE,
            "is_enabled": True,
            "last_error": "",
        },
    )
    messages.success(
        request,
        "试点笔记本已保存，请在来源详情页确认后启动首次同步。"
        if created
        else "笔记本同步设置已更新。",
    )
    return redirect("knowledge:source_detail", pk=source.pk)


@login_required
@require_POST
def microsoft_disconnect(request):
    member = current_member(request)
    if member is None:
        return _membership_required_response(request)
    connection = get_object_or_404(
        SourceConnection,
        family=member.family,
        member=member,
        provider=SourceConnection.PROVIDER_MICROSOFT,
    )
    connection.clear_token_cache()
    connection.available_notebooks = []
    connection.status = SourceConnection.STATUS_DISCONNECTED
    connection.last_error = "成员已主动断开 Microsoft 账户；NAS 已同步内容继续保留。"
    connection.save()
    connection.sources.update(
        status=KnowledgeSource.STATUS_DISCONNECTED,
        last_error="Microsoft 账户已断开，内容保留但不能继续同步。",
    )
    messages.success(request, "Microsoft 账户已断开，NAS 中已有内容没有删除。")
    return redirect("knowledge:sources")


@login_required
def source_detail(request, pk):
    member = current_member(request)
    if member is None:
        return _membership_required_response(request)
    source = get_object_or_404(visible_sources(member), pk=pk)
    return render(
        request,
        "knowledge/source_detail.html",
        {
            "source": source,
            "recent_jobs": source.jobs.select_related("requested_by").order_by("-created_at")[:10],
            "document_count": source.documents.count(),
            "source_sections": _source_sections(source)
            if source.kind == KnowledgeSource.KIND_ONENOTE
            else [],
            "route_choices": KnowledgeSource.ROUTE_CHOICES,
            "can_manage": _can_write(member) and can_manage_source(member, source),
            "can_change_settings": _can_write(member)
            and can_change_source_settings(member, source),
        },
    )


@login_required
@require_POST
def source_update(request, pk):
    member = current_member(request)
    if member is None:
        return _membership_required_response(request)
    source = get_object_or_404(KnowledgeSource, pk=pk, family=member.family)
    if not _can_write(member) or not can_change_source_settings(member, source):
        return HttpResponseForbidden("只有来源所有者可以修改来源设置和云端 AI 授权。")
    visibility = request.POST.get("visibility", "")
    if visibility not in KnowledgeVisibility.values:
        messages.error(request, "可见范围不正确。")
        return redirect("knowledge:source_detail", pk=source.pk)
    source.visibility = visibility
    source.allow_cloud_ai = request.POST.get("allow_cloud_ai") == "on"
    config = dict(source.config or {})
    if source.kind == KnowledgeSource.KIND_ONENOTE:
        valid_routes = dict(KnowledgeSource.ROUTE_CHOICES)
        default_route = request.POST.get("default_route", source.default_route)
        if default_route not in valid_routes:
            messages.error(request, "OneNote 默认处理方式不正确。")
            return redirect("knowledge:source_detail", pk=source.pk)
        section_routes = dict(config.get("section_routes") or {})
        section_ids = request.POST.getlist("section_id")
        section_values = request.POST.getlist("section_route")
        if len(section_ids) != len(section_values):
            messages.error(request, "OneNote 分区设置不完整，请刷新后重试。")
            return redirect("knowledge:source_detail", pk=source.pk)
        for section_id, route in zip(section_ids, section_values):
            if route not in valid_routes:
                messages.error(request, "OneNote 分区处理方式不正确。")
                return redirect("knowledge:source_detail", pk=source.pk)
            section_routes[str(section_id)] = route
        config["default_route"] = default_route
        config["section_routes"] = section_routes
    source.config = config
    source.save(
        update_fields=[
            "visibility",
            "allow_cloud_ai",
            "config",
            "updated_at",
        ]
    )
    updated_documents = 0
    if (
        source.kind == KnowledgeSource.KIND_ONENOTE
        and request.POST.get("apply_existing") == "on"
    ):
        for document in source.documents.iterator():
            section_id = (document.hierarchy or {}).get("section_id")
            desired_status = _knowledge_status_for_route(
                source.route_for_section(section_id)
            )
            if document.knowledge_status != desired_status:
                document.knowledge_status = desired_status
                document.save(update_fields=["knowledge_status", "updated_at"])
                updated_documents += 1
    messages.success(
        request,
        (
            f"来源设置已更新，并按分区规则更新了 {updated_documents} 篇已有文档。"
            if request.POST.get("apply_existing") == "on"
            else "来源设置已更新；分区规则只影响以后首次同步的页面。"
        ),
    )
    return redirect("knowledge:source_detail", pk=source.pk)


def _queue_source_job(request, source, job_type, parameters=None):
    member = current_member(request)
    if not _can_write(member) or not can_manage_source(member, source):
        return HttpResponseForbidden("只有来源所有者或家庭管理员可以运行该任务。")
    job, created = queue_knowledge_job(
        family=member.family,
        source=source,
        requested_by=member,
        job_type=job_type,
        parameters=parameters,
    )
    messages.success(
        request,
        "任务已加入队列。"
        if created
        else "该来源已有同类型任务正在排队或运行，未重复创建。",
    )
    return redirect("knowledge:job_detail", pk=job.pk)


@login_required
@require_POST
def source_sync(request, pk):
    member = current_member(request)
    if member is None:
        return _membership_required_response(request)
    source = get_object_or_404(KnowledgeSource, pk=pk, family=member.family)
    return _queue_source_job(
        request,
        source,
        KnowledgeJob.TYPE_SYNC_SOURCE,
        {"full_reconcile": request.POST.get("full_reconcile") == "on"},
    )


@login_required
@require_POST
def source_generate_proposals(request, pk):
    member = current_member(request)
    if member is None:
        return _membership_required_response(request)
    source = get_object_or_404(KnowledgeSource, pk=pk, family=member.family)
    if not source.allow_cloud_ai:
        messages.error(request, "请先明确勾选该来源允许发送正文给云端 AI。")
        return redirect("knowledge:source_detail", pk=source.pk)
    return _queue_source_job(
        request,
        source,
        KnowledgeJob.TYPE_GENERATE_PROPOSALS,
    )


def _visible_jobs(member):
    jobs = KnowledgeJob.objects.filter(family=member.family).select_related(
        "source",
        "requested_by",
    )
    if member.role == FamilyMember.ROLE_ADMIN:
        return jobs
    return jobs.filter(
        Q(requested_by=member)
        | Q(source__visibility=KnowledgeVisibility.FAMILY)
    )


@login_required
def jobs(request):
    member = current_member(request)
    if member is None:
        return _membership_required_response(request)
    page_obj = Paginator(_visible_jobs(member), 25).get_page(request.GET.get("page"))
    return render(request, "knowledge/jobs.html", {"page_obj": page_obj})


@login_required
def job_detail(request, pk):
    member = current_member(request)
    if member is None:
        return _membership_required_response(request)
    job = get_object_or_404(_visible_jobs(member), pk=pk)
    may_view_items = (
        job.source_id is None
        or job.source.owner_id == member.id
        or job.source.visibility == KnowledgeVisibility.FAMILY
    )
    return render(
        request,
        "knowledge/job_detail.html",
        {
            "job": job,
            "items": job.items.order_by("id") if may_view_items else job.items.none(),
            "items_redacted": not may_view_items,
            "can_manage": _can_write(member)
            and (
                job.requested_by_id == member.id
                or member.role == FamilyMember.ROLE_ADMIN
            ),
        },
    )


@login_required
@require_POST
def job_cancel(request, pk):
    member = current_member(request)
    if member is None:
        return _membership_required_response(request)
    job = get_object_or_404(_visible_jobs(member), pk=pk)
    if (
        not _can_write(member)
        or (
            job.requested_by_id != member.id
            and member.role != FamilyMember.ROLE_ADMIN
        )
    ):
        return HttpResponseForbidden("无权取消该任务。")
    if job.status == KnowledgeJob.STATUS_PENDING:
        now = timezone.now()
        job.status = KnowledgeJob.STATUS_CANCELLED
        job.finished_at = now
        job.heartbeat_at = now
        job.error_message = "任务在开始前由成员取消。"
        job.save(
            update_fields=[
                "status",
                "finished_at",
                "heartbeat_at",
                "error_message",
                "updated_at",
            ]
        )
        messages.success(request, "排队任务已取消。")
    elif job.status == KnowledgeJob.STATUS_RUNNING:
        job.status = KnowledgeJob.STATUS_CANCEL_REQUESTED
        job.save(update_fields=["status", "updated_at"])
        messages.success(request, "已请求取消，任务会在下一个安全点停止。")
    return redirect("knowledge:job_detail", pk=job.pk)


@login_required
@require_POST
def job_retry(request, pk):
    member = current_member(request)
    if member is None:
        return _membership_required_response(request)
    previous = get_object_or_404(_visible_jobs(member), pk=pk)
    if (
        not _can_write(member)
        or (
            previous.requested_by_id != member.id
            and member.role != FamilyMember.ROLE_ADMIN
        )
    ):
        return HttpResponseForbidden("无权重试该任务。")
    if previous.status not in {
        KnowledgeJob.STATUS_FAILED,
        KnowledgeJob.STATUS_PARTIAL,
        KnowledgeJob.STATUS_SOURCE_UNAVAILABLE,
        KnowledgeJob.STATUS_CANCELLED,
    }:
        messages.error(request, "当前任务状态不需要重试。")
        return redirect("knowledge:job_detail", pk=previous.pk)
    job, _ = queue_knowledge_job(
        family=previous.family,
        source=previous.source,
        requested_by=member,
        job_type=previous.job_type,
        parameters=previous.parameters,
    )
    messages.success(request, "已创建重试任务。")
    return redirect("knowledge:job_detail", pk=job.pk)


def _proposal_value(proposal, raw_value=None):
    if raw_value is None:
        suggested = proposal.suggested_value or {}
        if proposal.proposal_type == KnowledgeProposal.TYPE_TAGS:
            return suggested.get("items", [])
        return suggested.get("text", suggested.get("value", ""))
    if proposal.proposal_type == KnowledgeProposal.TYPE_TAGS:
        tags = []
        for value in re.split(r"[,，、\n]+", raw_value):
            tag = value.strip()
            if tag and tag not in tags:
                tags.append(tag[:30])
        return tags[:20]
    return raw_value.strip()


def _apply_proposal(proposal, member, *, accept, value=None):
    document = proposal.document
    if (
        proposal.status != KnowledgeProposal.STATUS_PENDING
        or proposal.revision_id != document.current_revision_id
        or proposal.content_hash != proposal.revision.content_hash
    ):
        raise ValueError("建议已过期或已经处理。")
    now = timezone.now()
    if accept:
        final_value = _proposal_value(proposal, value)
        if proposal.proposal_type == KnowledgeProposal.TYPE_SUMMARY:
            document.confirmed_summary = final_value
            changed_field = "confirmed_summary"
        elif proposal.proposal_type == KnowledgeProposal.TYPE_TAGS:
            document.tags = final_value
            changed_field = "tags"
        elif proposal.proposal_type == KnowledgeProposal.TYPE_CATEGORY:
            document.category = final_value
            changed_field = "category"
        proposal.status = KnowledgeProposal.STATUS_ACCEPTED
        proposal.human_value = {"value": final_value}
        document.save(update_fields=[changed_field, "updated_at"])
    else:
        proposal.status = KnowledgeProposal.STATUS_REJECTED
        proposal.human_value = {"reason": "member_rejected"}
    proposal.confirmed_by = member
    proposal.confirmed_at = now
    proposal.save(
        update_fields=["status", "human_value", "confirmed_by", "confirmed_at"]
    )
    if not document.proposals.filter(
        revision=document.current_revision,
        status=KnowledgeProposal.STATUS_PENDING,
    ).exists():
        document.curation_status = KnowledgeDocument.CURATION_CONFIRMED
        if document.knowledge_status == KnowledgeDocument.KNOWLEDGE_PENDING:
            document.knowledge_status = KnowledgeDocument.KNOWLEDGE_INCLUDED
        document.save(
            update_fields=[
                "curation_status",
                "knowledge_status",
                "updated_at",
            ]
        )
    document.refresh_from_db()
    index_document(document)


@login_required
def review(request):
    member = current_member(request)
    if member is None:
        return _membership_required_response(request)
    proposals = _manageable_proposals(member).filter(
        status=KnowledgeProposal.STATUS_PENDING
    ).order_by("document__content_modified_at", "document_id", "proposal_type")
    document_ids = list(
        proposals.order_by("document__content_modified_at", "document_id")
        .values_list("document_id", flat=True)
        .distinct()[:100]
    )
    requested_document_id = request.GET.get("document", "").strip()
    if requested_document_id.isdigit() and int(requested_document_id) in document_ids:
        selected_document_id = int(requested_document_id)
    else:
        selected_document_id = document_ids[0] if document_ids else None
    next_document_id = None
    if selected_document_id in document_ids:
        selected_index = document_ids.index(selected_document_id)
        if selected_index + 1 < len(document_ids):
            next_document_id = document_ids[selected_index + 1]
    review_document = None
    review_proposals = proposals.none()
    if selected_document_id:
        review_document = get_object_or_404(
            accessible_documents(member),
            pk=selected_document_id,
        )
        review_proposals = proposals.filter(document_id=selected_document_id)
    queue = list(
        accessible_documents(member)
        .filter(pk__in=document_ids)
        .annotate(
            pending_total=Count(
                "proposals",
                filter=Q(proposals__status=KnowledgeProposal.STATUS_PENDING),
            )
        )
        .order_by("content_modified_at", "id")[:30]
    )
    response = render(
        request,
        "knowledge/review.html",
        {
            "review_document": review_document,
            "review_proposals": review_proposals,
            "queue": queue,
            "pending_document_count": len(document_ids),
            "next_document_id": next_document_id,
            "can_write": _can_write(member),
        },
    )
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self'; style-src 'self'; "
        "script-src 'none'; object-src 'none'; frame-src 'none'; base-uri 'none'"
    )
    return response


@login_required
@require_POST
def proposal_review(request, pk):
    member = current_member(request)
    if member is None:
        return _membership_required_response(request)
    if not _can_write(member):
        return HttpResponseForbidden("只读成员不能确认整理建议。")
    proposal = get_object_or_404(_manageable_proposals(member), pk=pk)
    form = ProposalReviewForm(request.POST, proposal=proposal)
    if not form.is_valid():
        messages.error(request, "建议未处理：" + "；".join(form.non_field_errors() or ["请检查确认内容。"]))
        if request.POST.get("return_to") == "review":
            return redirect(reverse("knowledge:review") + f"?document={proposal.document_id}")
        return redirect("knowledge:document_detail", pk=proposal.document_id)
    try:
        with transaction.atomic():
            _apply_proposal(
                proposal,
                member,
                accept=form.cleaned_data["action"] == ProposalReviewForm.ACTION_ACCEPT,
                value=form.cleaned_data["value"],
            )
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "整理建议已记录。")
    if request.POST.get("return_to") == "review":
        return redirect("knowledge:review")
    return redirect("knowledge:document_detail", pk=proposal.document_id)


def _bulk_proposals(member, ids):
    proposals = list(
        _manageable_proposals(member)
        .filter(pk__in=ids, status=KnowledgeProposal.STATUS_PENDING)
        .order_by("document__title", "proposal_type")
    )
    if len(proposals) != len(set(ids)):
        raise ValueError("部分建议无权访问、已经处理或已经过期，请刷新后重试。")
    return proposals


@login_required
@require_POST
def proposal_bulk_preview(request):
    member = current_member(request)
    if member is None:
        return _membership_required_response(request)
    if not _can_write(member):
        return HttpResponseForbidden("只读成员不能批量确认建议。")
    selected = request.POST.getlist("selected")
    form = BulkProposalPreviewForm({"proposal_ids": ",".join(selected)})
    if not form.is_valid():
        messages.error(request, "请选择需要批量确认的建议。")
        return redirect("knowledge:review")
    try:
        proposals = _bulk_proposals(member, form.cleaned_data["proposal_ids"])
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect("knowledge:review")
    return render(
        request,
        "knowledge/bulk_preview.html",
        {
            "proposals": proposals,
            "proposal_ids": ",".join(str(item.pk) for item in proposals),
        },
    )


@login_required
@require_POST
def proposal_bulk_apply(request):
    member = current_member(request)
    if member is None:
        return _membership_required_response(request)
    if not _can_write(member):
        return HttpResponseForbidden("只读成员不能批量确认建议。")
    form = BulkProposalPreviewForm(request.POST)
    if not form.is_valid() or request.POST.get("confirm") != "yes":
        messages.error(request, "批量确认信息无效，请重新预览。")
        return redirect("knowledge:review")
    try:
        with transaction.atomic():
            proposals = _bulk_proposals(member, form.cleaned_data["proposal_ids"])
            for proposal in proposals:
                _apply_proposal(proposal, member, accept=True)
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"已批量接受 {len(proposals)} 项建议。")
    return redirect("knowledge:review")
