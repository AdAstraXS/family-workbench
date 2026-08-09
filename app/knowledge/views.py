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

from .ai import KnowledgeAiError, knowledge_ai_provider
from .forms import (
    BulkProposalPreviewForm,
    DocumentOrganizeForm,
    KnowledgeCategoryForm,
    KnowledgeImportUploadForm,
    KnowledgeTagForm,
    NotebookSelectionForm,
    ProposalReviewForm,
    TaxonomyMergeForm,
)
from .imports import KnowledgeImportError, create_uploaded_import_batch
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
    KnowledgeCategory,
    KnowledgeCurationRevision,
    KnowledgeDocument,
    KnowledgeImportBatch,
    KnowledgeJob,
    KnowledgeProposal,
    KnowledgeSearchEntry,
    KnowledgeSource,
    KnowledgeTag,
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
from .services import (
    mark_ai_processing_documents,
    queue_knowledge_job,
    record_curation_revision,
    restore_ai_processing_documents,
)
from .taxonomy import (
    category_usage,
    ensure_category,
    ensure_tags,
    merge_category,
    merge_tag,
    rename_category,
    rename_tag,
    tag_usage,
)


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


def _decorate_entries(entries, query="", member=None):
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
        if entry.curation_status == KnowledgeDocument.CURATION_PENDING_REVIEW:
            entry.workflow_stage = "waiting_review"
            entry.workflow_stage_label = "等待确认"
        elif entry.curation_status == KnowledgeDocument.CURATION_PENDING_AI:
            entry.workflow_stage = "processing"
            entry.workflow_stage_label = "AI 整理中"
        else:
            entry.workflow_stage = "unorganized"
            entry.workflow_stage_label = "尚未整理"
        entry.can_ai_organize = bool(
            member
            and entry.document_id
            and entry.workflow_stage == "unorganized"
            and _can_write(member)
            and can_organize_document(member, entry.document)
        )
    return entries


def _formal_entries(entries):
    return entries.filter(
        knowledge_status__in=[
            KnowledgeDocument.KNOWLEDGE_INCLUDED,
            KnowledgeDocument.KNOWLEDGE_PENDING,
        ]
    )


def _curated_entries(entries):
    return entries.filter(
        knowledge_status=KnowledgeDocument.KNOWLEDGE_INCLUDED,
    ).filter(
        Q(item_kind=KnowledgeSearchEntry.KIND_INVESTMENT_NOTE)
        | Q(document__library_tier=KnowledgeDocument.LIBRARY_KNOWLEDGE)
    )


def _knowledge_stats(member):
    entries = accessible_search_entries(member).filter(owner=member)
    documents = accessible_documents(member).filter(owner=member)
    today = timezone.localdate()
    return {
        "total": _curated_entries(entries).count(),
        "today_new": _curated_entries(entries).filter(created_at__date=today).count(),
        "inbox": entries.filter(
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
    groups = []

    onenote_nodes = list(
        entries.filter(
            item_kind=KnowledgeSearchEntry.KIND_DOCUMENT,
            source_kind=KnowledgeSource.KIND_ONENOTE,
            document__isnull=False,
        )
        .order_by()
        .values("document__source_id", "source_name")
        .annotate(count=Count("id"))
        .order_by("source_name")
    )
    if onenote_nodes:
        groups.append(
            {
                "id": "onenote",
                "name": "OneNote",
                "count": sum(item["count"] for item in onenote_nodes),
                "nodes": [
                    {
                        "name": item["source_name"],
                        "count": item["count"],
                        "source_id": str(item["document__source_id"]),
                        "author": "",
                    }
                    for item in onenote_nodes
                ],
            }
        )

    note_nodes = list(
        entries.filter(item_kind=KnowledgeSearchEntry.KIND_INVESTMENT_NOTE)
        .order_by()
        .values("author_name")
        .annotate(count=Count("id"))
        .order_by("author_name")
    )
    if note_nodes:
        groups.append(
            {
                "id": "notes",
                "name": "随手记",
                "count": sum(item["count"] for item in note_nodes),
                "nodes": [
                    {
                        "name": item["author_name"] or "作者未标注",
                        "count": item["count"],
                        "source_id": "",
                        "author": item["author_name"],
                    }
                    for item in note_nodes
                ],
            }
        )

    people_rows = list(
        entries.filter(
            item_kind=KnowledgeSearchEntry.KIND_DOCUMENT,
            source_kind__in=[
                KnowledgeSource.KIND_HTML_IMPORT,
                KnowledgeSource.KIND_MARKDOWN_IMPORT,
            ],
        )
        .order_by()
        .values("author_name", "source_name")
        .annotate(count=Count("id"))
        .order_by("author_name", "source_name")
    )
    people_nodes = {}
    for item in people_rows:
        author = (item["author_name"] or item["source_name"] or "作者未标注").strip()
        node = people_nodes.setdefault(
            author,
            {"name": author, "count": 0, "source_id": "", "author": author},
        )
        node["count"] += item["count"]
    if people_nodes:
        ordered_people = sorted(people_nodes.values(), key=lambda item: item["name"])
        groups.append(
            {
                "id": "people",
                "name": "关注人物",
                "count": sum(item["count"] for item in ordered_people),
                "nodes": ordered_people,
            }
        )

    other_nodes = list(
        entries.filter(
            item_kind=KnowledgeSearchEntry.KIND_DOCUMENT,
            document__isnull=False,
        )
        .exclude(
            source_kind__in=[
                KnowledgeSource.KIND_ONENOTE,
                KnowledgeSource.KIND_HTML_IMPORT,
                KnowledgeSource.KIND_MARKDOWN_IMPORT,
            ]
        )
        .order_by()
        .values("document__source_id", "source_name")
        .annotate(count=Count("id"))
        .order_by("source_name")
    )
    if other_nodes:
        groups.append(
            {
                "id": "other",
                "name": "其他来源",
                "count": sum(item["count"] for item in other_nodes),
                "nodes": [
                    {
                        "name": item["source_name"],
                        "count": item["count"],
                        "source_id": str(item["document__source_id"]),
                        "author": "",
                    }
                    for item in other_nodes
                ],
            }
        )
    return groups


@login_required
def index(request):
    member = current_member(request)
    if member is None:
        return _membership_required_response(request)

    entries = _curated_entries(
        accessible_search_entries(member).filter(owner=member)
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
    forced_source_id=None,
    forced_collection=None,
    *,
    page_title="资料库",
    page_description="归档资料保存全部正式内容；待整理和精选知识是归档资料中的整理状态。默认显示当前成员的精选知识。",
):
    entries = accessible_search_entries(member)
    display_mode_cookie_name = f"knowledge_library_display_mode_{member.pk}"
    display_mode = request.COOKIES.get(display_mode_cookie_name, "standard").strip().lower()
    if display_mode not in {"standard", "compact", "cards"}:
        display_mode = "standard"
    query = request.GET.get("q", "").strip()
    collection = request.GET.get("collection", "curated").strip()
    if collection == "library":
        collection = "archive"
    if collection not in {"all", "curated", "pending", "archive"}:
        collection = "curated"
    if forced_collection in {"all", "curated", "pending", "archive"}:
        collection = forced_collection
    if (
        query
        and forced_collection is None
        and request.GET.get("search_scope", "archive") == "archive"
    ):
        collection = "archive"
    if collection == "curated":
        entries = _curated_entries(entries)
    elif collection == "pending":
        entries = entries.filter(
            knowledge_status=KnowledgeDocument.KNOWLEDGE_PENDING,
        )
    elif collection == "archive":
        entries = _formal_entries(entries)
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
    source_group = request.GET.get("source_group", "").strip()
    source_author = request.GET.get("source_author", "").strip()
    if forced_source_id is not None:
        source_id = str(forced_source_id)
    section = request.GET.get("section", "").strip()
    section_group = request.GET.get("section_group", "").strip()
    quick_filter = request.GET.get("quick", "").strip()
    workflow_stage = request.GET.get("stage", "").strip()
    date_from = request.GET.get("date_from", "").strip()
    date_to = request.GET.get("date_to", "").strip()

    if directory_mode not in {"category", "source"}:
        directory_mode = "category"
    if source_group not in {"onenote", "notes", "people", "other"}:
        source_group = ""
    if source_id == "notes":
        source_group = "notes"
        source_id = ""

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

    if forced_source_id is not None:
        entries = entries.filter(
            item_kind=KnowledgeSearchEntry.KIND_DOCUMENT,
            document__source_id=int(forced_source_id),
        )

    directory_entries = entries
    directory_total = directory_entries.count()
    workflow_counts = {
        "all": directory_total,
        "unorganized": directory_entries.filter(
            curation_status__in=[
                KnowledgeDocument.CURATION_INBOX,
                KnowledgeDocument.CURATION_NORMALIZED,
            ]
        ).count(),
        "processing": directory_entries.filter(
            curation_status=KnowledgeDocument.CURATION_PENDING_AI,
        ).count(),
        "waiting_review": directory_entries.filter(
            curation_status=KnowledgeDocument.CURATION_PENDING_REVIEW,
        ).count(),
    }
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
    if source_group == "onenote":
        entries = entries.filter(source_kind=KnowledgeSource.KIND_ONENOTE)
    elif source_group == "notes":
        entries = entries.filter(
            item_kind=KnowledgeSearchEntry.KIND_INVESTMENT_NOTE
        )
    elif source_group == "people":
        entries = entries.filter(
            source_kind__in=[
                KnowledgeSource.KIND_HTML_IMPORT,
                KnowledgeSource.KIND_MARKDOWN_IMPORT,
            ]
        )
    elif source_group == "other":
        entries = entries.exclude(
            source_kind__in=[
                KnowledgeSource.KIND_ONENOTE,
                KnowledgeSource.KIND_INTERNAL_NOTES,
                KnowledgeSource.KIND_HTML_IMPORT,
                KnowledgeSource.KIND_MARKDOWN_IMPORT,
            ]
        )
    if source_author:
        entries = entries.filter(author_name=source_author)
    if source_id.isdigit():
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
    elif source_id:
        source_id = ""
        section = ""
        section_group = ""

    if quick_filter == "recent":
        entries = entries.filter(content_time__gte=timezone.now() - timedelta(days=30))
    elif quick_filter == "uncategorized":
        entries = entries.filter(category="")
    else:
        quick_filter = ""
    if collection == "pending" and workflow_stage == "unorganized":
        entries = entries.filter(
            curation_status__in=[
                KnowledgeDocument.CURATION_INBOX,
                KnowledgeDocument.CURATION_NORMALIZED,
            ]
        )
    elif collection == "pending" and workflow_stage == "waiting_review":
        entries = entries.filter(
            curation_status=KnowledgeDocument.CURATION_PENDING_REVIEW,
        )
    else:
        workflow_stage = ""
    if curation_status and collection != "pending":
        entries = entries.filter(curation_status=curation_status)
    elif collection == "pending":
        curation_status = ""
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
    _decorate_entries(page_obj.object_list, query, member)
    ai_selectable_count = sum(
        1 for entry in page_obj.object_list if entry.can_ai_organize
    )
    pagination_params = request.GET.copy()
    pagination_params.pop("page", None)

    source_labels = dict(KnowledgeSource.KIND_CHOICES)
    source_choices = list(
        directory_entries.order_by()
        .values("source_kind")
        .annotate(total=Count("id"))
        .order_by("source_kind")
    )
    for item in source_choices:
        item["label"] = source_labels.get(item["source_kind"], item["source_kind"])
    selected_source_name = ""
    for group in source_directory:
        group["is_selected"] = group["id"] == source_group
        if group["id"] == source_group and not source_id and not source_author:
            selected_source_name = group["name"]
        for node in group["nodes"]:
            node["is_selected"] = False
            if source_id and node["source_id"] == source_id:
                selected_source_name = node["name"]
                group["is_selected"] = True
                node["is_selected"] = True
            elif (
                source_author
                and group["id"] == source_group
                and node["author"] == source_author
            ):
                selected_source_name = node["name"]
                group["is_selected"] = True
                node["is_selected"] = True
    if forced_source_id is not None and not selected_source_name:
        selected_source_name = (
            KnowledgeSource.objects.filter(pk=forced_source_id)
            .values_list("name", flat=True)
            .first()
            or ""
        )
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
        directory_title = {
            "all": "全部资料（含仅同步）",
            "archive": "归档资料",
            "pending": "全部待整理",
            "curated": "精选知识",
        }[collection]
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
            "selected_source_group": source_group,
            "selected_source_author": source_author,
            "selected_source_name": selected_source_name,
            "selected_section": section,
            "selected_section_group": section_group,
            "quick_filter": quick_filter,
            "selected_stage": workflow_stage,
            "workflow_counts": workflow_counts,
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
            "directory_view_name": (
                "knowledge:inbox"
                if collection == "pending"
                else "knowledge:library"
            ),
            "pagination_query": pagination_params.urlencode(),
            "ai_selectable_count": ai_selectable_count,
            "display_mode": display_mode,
            "display_mode_cookie_name": display_mode_cookie_name,
            "display_mode_choices": [
                {"value": "standard", "label": "标准列表"},
                {"value": "compact", "label": "紧凑表格"},
                {"value": "cards", "label": "卡片"},
            ],
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
    return _library_response(
        request,
        member,
        forced_collection="pending",
        page_title="待整理",
        page_description="这里只放已经准备进入精选知识、但尚未完成摘要、分类或标签整理的资料；可以手工整理，也可以使用 AI 辅助。",
    )


@login_required
def topics(request):
    member = current_member(request)
    if member is None:
        return _membership_required_response(request)
    entries = _curated_entries(accessible_search_entries(member))
    tag_counts = Counter()
    for tags in entries.values_list("tags", flat=True)[:5000]:
        tag_counts.update(str(tag).strip() for tag in (tags or []) if str(tag).strip())
    category_counts = {
        item["category"]: item["total"]
        for item in entries.exclude(category="")
        .order_by()
        .values("category")
        .annotate(total=Count("id"))
    }
    categories = list(
        KnowledgeCategory.objects.filter(family=member.family)
        .select_related("merged_into")
        .order_by("-is_active", "name", "id")
    )
    tags = list(
        KnowledgeTag.objects.filter(family=member.family)
        .select_related("merged_into")
        .order_by("-is_active", "name", "id")
    )
    for item in categories:
        item.visible_usage_count = category_counts.get(item.name, 0)
    for item in tags:
        item.visible_usage_count = tag_counts.get(item.name, 0)
    category_catalog_names = {item.name for item in categories}
    for name, total in sorted(category_counts.items()):
        if name not in category_catalog_names:
            categories.append(
                {
                    "name": name,
                    "visible_usage_count": total,
                    "is_active": True,
                    "description": "既有资料使用的分类，编辑资料后会自动纳入目录。",
                }
            )
    tag_catalog_names = {item.name for item in tags}
    for name, total in tag_counts.most_common():
        if name not in tag_catalog_names:
            tags.append(
                {
                    "name": name,
                    "visible_usage_count": total,
                    "is_active": True,
                    "description": "既有资料使用的标签，编辑资料后会自动纳入目录。",
                }
            )
    return render(
        request,
        "knowledge/topics.html",
        {
            "tags": tags,
            "categories": categories,
            "entry_count": entries.count(),
            "can_manage_taxonomy": member.role == FamilyMember.ROLE_ADMIN,
        },
    )


def _taxonomy_config(kind):
    if kind == "category":
        return {
            "model": KnowledgeCategory,
            "form": KnowledgeCategoryForm,
            "label": "分类",
            "rename": rename_category,
            "merge": merge_category,
            "usage": category_usage,
        }
    if kind == "tag":
        return {
            "model": KnowledgeTag,
            "form": KnowledgeTagForm,
            "label": "标签",
            "rename": rename_tag,
            "merge": merge_tag,
            "usage": tag_usage,
        }
    raise Http404("未知的分类或标签类型。")


def _taxonomy_admin_member(request):
    member = current_member(request)
    if member is None:
        return None, _membership_required_response(request)
    if member.role != FamilyMember.ROLE_ADMIN:
        return member, HttpResponseForbidden("只有家庭管理员可以维护分类与标签目录。")
    return member, None


@login_required
def taxonomy_create(request, kind):
    member, denied = _taxonomy_admin_member(request)
    if denied:
        return denied
    config = _taxonomy_config(kind)
    form = config["form"](request.POST or None, family=member.family)
    if request.method == "POST" and form.is_valid():
        item = form.save(commit=False)
        item.family = member.family
        item.created_by = member
        item.save()
        messages.success(request, f"已新增{config['label']}“{item.name}”。")
        return redirect("knowledge:topics")
    return render(
        request,
        "knowledge/taxonomy_form.html",
        {"form": form, "label": config["label"], "mode": "create"},
    )


@login_required
def taxonomy_edit(request, kind, pk):
    member, denied = _taxonomy_admin_member(request)
    if denied:
        return denied
    config = _taxonomy_config(kind)
    item = get_object_or_404(config["model"], family=member.family, pk=pk)
    original_name = item.name
    form = config["form"](request.POST or None, instance=item, family=member.family)
    if request.method == "POST" and form.is_valid():
        new_name = form.cleaned_data["name"]
        if original_name != new_name:
            item.name = original_name
            config["rename"](item, new_name)
            item.refresh_from_db()
        item.description = form.cleaned_data["description"]
        item.is_active = form.cleaned_data["is_active"]
        item.save(update_fields=["description", "is_active", "updated_at"])
        messages.success(request, f"已更新{config['label']}“{item.name}”。")
        return redirect("knowledge:topics")
    return render(
        request,
        "knowledge/taxonomy_form.html",
        {"form": form, "label": config["label"], "mode": "edit", "item": item},
    )


@login_required
def taxonomy_delete(request, kind, pk):
    member, denied = _taxonomy_admin_member(request)
    if denied:
        return denied
    config = _taxonomy_config(kind)
    item = get_object_or_404(config["model"], family=member.family, pk=pk)
    usage = config["usage"](item)
    has_merge_history = (
        item.merged_categories.exists()
        if kind == "category"
        else item.merged_tags.exists()
    )
    if request.method != "POST":
        return render(
            request,
            "knowledge/taxonomy_delete.html",
            {
                "item": item,
                "kind": kind,
                "label": config["label"],
                "usage": usage,
                "has_merge_history": has_merge_history,
            },
        )
    if usage:
        messages.error(
            request,
            f"“{item.name}”仍被 {usage} 篇资料使用，不能删除；请停用或合并。",
        )
    elif has_merge_history:
        messages.error(request, "该项目保存着历史合并关系，不能删除，可以保持停用。")
    else:
        name = item.name
        item.delete()
        messages.success(request, f"已删除未使用的{config['label']}“{name}”。")
    return redirect("knowledge:topics")


@login_required
def taxonomy_merge(request, kind, pk):
    member, denied = _taxonomy_admin_member(request)
    if denied:
        return denied
    config = _taxonomy_config(kind)
    item = get_object_or_404(
        config["model"], family=member.family, pk=pk, merged_into__isnull=True
    )
    form = TaxonomyMergeForm(request.POST or None, item=item)
    if request.method == "POST" and form.is_valid():
        target = form.cleaned_data["target"]
        config["merge"](item, target)
        messages.success(
            request,
            f"已把{config['label']}“{item.name}”合并到“{target.name}”。",
        )
        return redirect("knowledge:topics")
    return render(
        request,
        "knowledge/taxonomy_merge.html",
        {"form": form, "label": config["label"], "item": item},
    )


@login_required
def people(request):
    member = current_member(request)
    if member is None:
        return _membership_required_response(request)
    entries = _formal_entries(accessible_search_entries(member))
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
    historical_people = list(
        entries.filter(
            source_kind__in=[
                KnowledgeSource.KIND_HTML_IMPORT,
                KnowledgeSource.KIND_MARKDOWN_IMPORT,
            ]
        )
        .exclude(author_name="")
        .order_by()
        .values("author_name")
        .annotate(history_count=Count("id"))
        .order_by("author_name")
    )
    return render(
        request,
        "knowledge/people.html",
        {
            "authors": authors,
            "selected_person": selected_person,
            "timeline": timeline,
            "historical_people": historical_people,
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
    stored_reading = (member.extra_data or {}).get("knowledge_reading") or {}
    font_size = request.GET.get(
        "font_size",
        stored_reading.get("font_size", "medium"),
    ).strip().lower()
    if font_size not in {"small", "medium", "large"}:
        font_size = "medium"
    line_spacing = request.GET.get(
        "line_spacing",
        stored_reading.get("line_spacing", "normal"),
    ).strip().lower()
    if line_spacing not in {"compact", "normal"}:
        line_spacing = "normal"
    proposals = list(
        document.proposals.filter(revision=document.current_revision)
        .select_related("confirmed_by", "run")
        .order_by("-run__sequence", "proposal_type", "id")
    )
    pending_proposals = [
        proposal
        for proposal in proposals
        if proposal.status == KnowledgeProposal.STATUS_PENDING
    ]
    proposal_runs = list(
        document.proposal_runs.select_related("revision")
        .prefetch_related("proposals__confirmed_by")
        .order_by("-sequence", "-id")
    )
    history_runs = []
    for proposal_run in proposal_runs:
        run_proposals = list(proposal_run.proposals.all())
        proposal_run.accepted_count = sum(
            item.status == KnowledgeProposal.STATUS_ACCEPTED for item in run_proposals
        )
        proposal_run.rejected_count = sum(
            item.status == KnowledgeProposal.STATUS_REJECTED for item in run_proposals
        )
        proposal_run.stale_count = sum(
            item.status == KnowledgeProposal.STATUS_STALE for item in run_proposals
        )
        if not any(
            item.status == KnowledgeProposal.STATUS_PENDING for item in run_proposals
        ):
            history_runs.append(proposal_run)
    confirmed_summary_proposal = next(
        (
            proposal
            for proposal in document.proposals.filter(
                proposal_type=KnowledgeProposal.TYPE_SUMMARY,
                status=KnowledgeProposal.STATUS_ACCEPTED,
                confirmed_by__isnull=False,
            ).select_related("confirmed_by").order_by("-confirmed_at", "-id")
            if proposal.proposal_type == KnowledgeProposal.TYPE_SUMMARY
        ),
        None,
    )
    response = render(
        request,
        "knowledge/document_detail.html",
        {
            "document": document,
            "revision": document.current_revision,
            "proposals": proposals,
            "pending_proposals": pending_proposals,
            "history_runs": history_runs,
            "confirmed_summary_proposal": confirmed_summary_proposal,
            "reading_font_size": font_size,
            "reading_line_spacing": line_spacing,
            "reading_font_size_options": [
                {"value": "small", "label": "小"},
                {"value": "medium", "label": "中"},
                {"value": "large", "label": "大"},
            ],
            "reading_line_spacing_options": [
                {"value": "compact", "label": "紧凑"},
                {"value": "normal", "label": "正常"},
            ],
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
@require_POST
def document_reading_preferences(request, pk):
    member = current_member(request)
    if member is None:
        return _membership_required_response(request)
    get_object_or_404(accessible_documents(member), pk=pk)
    font_size = request.POST.get("font_size", "medium").strip().lower()
    line_spacing = request.POST.get("line_spacing", "normal").strip().lower()
    if font_size not in {"small", "medium", "large"}:
        font_size = "medium"
    if line_spacing not in {"compact", "normal"}:
        line_spacing = "normal"
    extra_data = dict(member.extra_data or {})
    extra_data["knowledge_reading"] = {
        "font_size": font_size,
        "line_spacing": line_spacing,
    }
    member.extra_data = extra_data
    member.save(update_fields=["extra_data", "updated_at"])
    return redirect("knowledge:document_detail", pk=pk)


@login_required
@require_POST
def document_add_to_inbox(request, pk):
    member = current_member(request)
    if member is None:
        return _membership_required_response(request)
    document = get_object_or_404(accessible_documents(member), pk=pk)
    if not _can_write(member) or not can_organize_document(member, document):
        return HttpResponseForbidden("只有资料所有者或家庭管理员可以整理这项资料。")
    if document.knowledge_status == KnowledgeDocument.KNOWLEDGE_ARCHIVED:
        messages.error(request, "这项内容目前仅同步保存，尚未进入归档资料。")
    elif document.library_tier == KnowledgeDocument.LIBRARY_KNOWLEDGE:
        messages.info(request, "这项内容已经是精选知识。")
    else:
        document.knowledge_status = KnowledgeDocument.KNOWLEDGE_PENDING
        document.library_tier = KnowledgeDocument.LIBRARY_ARCHIVE
        document.curation_status = KnowledgeDocument.CURATION_INBOX
        document.save(
            update_fields=[
                "knowledge_status",
                "library_tier",
                "curation_status",
                "updated_at",
            ]
        )
        index_document(document)
        messages.success(request, "已加入待整理；原文仍保留在归档资料中。")
    return redirect("knowledge:document_detail", pk=document.pk)


@login_required
@require_POST
def document_cancel_organizing(request, pk):
    member = current_member(request)
    if member is None:
        return _membership_required_response(request)
    document = get_object_or_404(accessible_documents(member), pk=pk)
    if not _can_write(member) or not can_organize_document(member, document):
        return HttpResponseForbidden("只有资料所有者或家庭管理员可以整理这项资料。")
    if document.knowledge_status == KnowledgeDocument.KNOWLEDGE_PENDING:
        document.knowledge_status = KnowledgeDocument.KNOWLEDGE_INCLUDED
        document.library_tier = KnowledgeDocument.LIBRARY_ARCHIVE
        document.curation_status = KnowledgeDocument.CURATION_NORMALIZED
        document.save(
            update_fields=[
                "knowledge_status",
                "library_tier",
                "curation_status",
                "updated_at",
            ]
        )
        index_document(document)
        messages.success(request, "已取消整理；资料继续保留在归档资料中。")
    return redirect("knowledge:document_detail", pk=document.pk)


def _selected_ai_documents(member, raw_ids, *, mode="pending_documents"):
    document_ids = []
    for raw_id in raw_ids:
        value = str(raw_id).strip()
        if value.isdigit() and int(value) not in document_ids:
            document_ids.append(int(value))
    if not document_ids:
        raise KnowledgeAiError("请先选择至少一篇尚未整理的资料。")
    if mode == "curated_reorganization" and len(document_ids) != 1:
        raise KnowledgeAiError("精选知识重新整理目前一次只处理一篇资料。")
    if len(document_ids) > 100:
        raise KnowledgeAiError("一次最多选择 100 篇资料进行 AI 整理。")
    queryset = accessible_documents(member).filter(
        pk__in=document_ids,
        current_revision__isnull=False,
        sync_status=KnowledgeDocument.SYNC_AVAILABLE,
    )
    if mode == "curated_reorganization":
        queryset = queryset.filter(
            knowledge_status=KnowledgeDocument.KNOWLEDGE_INCLUDED,
            library_tier=KnowledgeDocument.LIBRARY_KNOWLEDGE,
            curation_status=KnowledgeDocument.CURATION_CONFIRMED,
        )
    else:
        queryset = queryset.filter(
            knowledge_status=KnowledgeDocument.KNOWLEDGE_PENDING,
            curation_status__in=[
                KnowledgeDocument.CURATION_INBOX,
                KnowledgeDocument.CURATION_NORMALIZED,
            ],
        )
    documents = list(queryset.order_by("source__name", "title", "id"))
    if len(documents) != len(document_ids):
        raise KnowledgeAiError(
            "部分资料当前不能发起 AI 整理，可能正在处理、等待确认或状态已经变化。"
        )
    if any(not can_organize_document(member, document) for document in documents):
        raise KnowledgeAiError("只能为自己有权整理的资料创建 AI 任务。")
    return documents


def _can_authorize_ai_once(member, source, documents):
    if source.allow_cloud_ai or source.owner_id == member.id:
        return True
    return source.owner_id is None and all(
        document.owner_id == member.id for document in documents
    )


def _ai_document_groups(member, documents):
    grouped = {}
    for document in documents:
        grouped.setdefault(document.source_id, []).append(document)
    groups = []
    for source_id, source_documents in grouped.items():
        source = source_documents[0].source
        if not _can_authorize_ai_once(member, source, source_documents):
            raise KnowledgeAiError(
                f"“{source.name}”尚未获得来源所有者的云端 AI 授权，"
                "家庭管理员不能代替其他成员授权。"
            )
        provider = knowledge_ai_provider(source)
        provider_extra = provider.extra_data or {}
        groups.append(
            {
                "source": source,
                "documents": source_documents,
                "provider": provider,
                "character_count": sum(
                    len(document.current_revision.plain_text or "")
                    for document in source_documents
                ),
                "retention_policy": provider_extra.get("data_retention_policy")
                or "以服务商当前条款为准",
                "cost_limit": provider_extra.get("knowledge_cost_limit")
                or "尚未配置单独费用上限",
                "can_authorize_source": can_change_source_settings(member, source),
            }
        )
    return sorted(groups, key=lambda item: (item["source"].name, item["source"].pk))


@login_required
@require_POST
def document_ai_organize(request):
    member = current_member(request)
    if member is None:
        return _membership_required_response(request)
    if not _can_write(member):
        return HttpResponseForbidden("只读成员不能创建 AI 整理任务。")
    mode = request.POST.get("mode", "pending_documents").strip()
    if mode not in {"pending_documents", "curated_reorganization"}:
        mode = "pending_documents"
    raw_return_document_id = request.POST.get("return_document_id", "").strip()

    def error_redirect():
        if raw_return_document_id.isdigit():
            return redirect(
                "knowledge:document_detail", pk=int(raw_return_document_id)
            )
        return redirect("knowledge:inbox")

    try:
        documents = _selected_ai_documents(
            member,
            request.POST.getlist("document_ids"),
            mode=mode,
        )
        groups = _ai_document_groups(member, documents)
    except KnowledgeAiError as exc:
        messages.error(request, str(exc))
        return error_redirect()

    return_document_id = raw_return_document_id
    if not return_document_id.isdigit() or int(return_document_id) not in {
        document.pk for document in documents
    }:
        return_document_id = ""
    unauthorized_groups = [
        group for group in groups if not group["source"].allow_cloud_ai
    ]
    if request.POST.get("confirm") != "yes":
        return render(
            request,
            "knowledge/ai_organize_confirm.html",
            {
                "documents": documents,
                "groups": groups,
                "unauthorized_groups": unauthorized_groups,
                "document_count": len(documents),
                "return_document_id": return_document_id,
                "mode": mode,
                "is_reorganization": mode == "curated_reorganization",
                "can_authorize_all_sources": all(
                    group["can_authorize_source"] for group in unauthorized_groups
                ),
            },
        )

    authorization = request.POST.get("authorization", "existing")
    if request.POST.get("acknowledge") != "yes":
        messages.error(request, "请先确认本次正文发送范围和人工核对责任。")
        return error_redirect()
    if unauthorized_groups and authorization not in {"once", "source"}:
        messages.error(request, "请选择本次 AI 正文发送授权方式。")
        return error_redirect()
    if authorization == "source" and any(
        not group["can_authorize_source"] for group in unauthorized_groups
    ):
        return HttpResponseForbidden("只能由来源所有者设置今后持续授权。")

    source_ids = [group["source"].pk for group in groups]
    active_job = (
        KnowledgeJob.objects.filter(
            source_id__in=source_ids,
            job_type=KnowledgeJob.TYPE_GENERATE_PROPOSALS,
            status__in=KnowledgeJob.ACTIVE_STATUSES,
        )
        .select_related("source")
        .first()
    )
    if active_job:
        messages.error(
            request,
            f"“{active_job.source.name}”已有 AI 整理任务正在排队或运行，"
            "请完成后再选择下一批。",
        )
        return error_redirect()

    jobs = []
    try:
        with transaction.atomic():
            if authorization == "source":
                for group in unauthorized_groups:
                    group["source"].allow_cloud_ai = True
                    group["source"].save(
                        update_fields=["allow_cloud_ai", "updated_at"]
                    )
            for group in groups:
                document_ids = [document.pk for document in group["documents"]]
                parameters = {
                    "document_ids": document_ids,
                    "selection_scope": mode,
                }
                if not group["source"].allow_cloud_ai and authorization == "once":
                    parameters["one_time_document_ids"] = document_ids
                job, created = queue_knowledge_job(
                    family=member.family,
                    source=group["source"],
                    requested_by=member,
                    job_type=KnowledgeJob.TYPE_GENERATE_PROPOSALS,
                    parameters=parameters,
                )
                if not created:
                    raise KnowledgeAiError(
                        f"“{group['source'].name}”刚刚创建了另一项 AI 任务，"
                        "请稍后重试。"
                    )
                job.total_count = len(document_ids)
                job.save(update_fields=["total_count", "updated_at"])
                mark_ai_processing_documents(job)
                jobs.append(job)
    except KnowledgeAiError as exc:
        messages.error(request, str(exc))
        return error_redirect()

    messages.success(
        request,
        (
            "已创建精选知识重新整理任务；当前精选结果保持不变，"
            "新建议生成后等待你确认。"
            if mode == "curated_reorganization"
            else f"已为 {len(documents)} 篇资料创建 {len(jobs)} 个 AI 整理任务；"
            "完成后仍留在待整理，并进入“等待确认”。"
        ),
    )
    if return_document_id:
        return redirect("knowledge:document_detail", pk=int(return_document_id))
    return redirect("knowledge:inbox")


@login_required
def document_organize(request, pk):
    member = current_member(request)
    if member is None:
        return _membership_required_response(request)
    document = get_object_or_404(accessible_documents(member), pk=pk)
    if not _can_write(member) or not can_organize_document(member, document):
        return HttpResponseForbidden("只有资料所有者或家庭管理员可以整理这项知识。")
    if (
        document.knowledge_status != KnowledgeDocument.KNOWLEDGE_PENDING
        and document.library_tier != KnowledgeDocument.LIBRARY_KNOWLEDGE
    ):
        messages.info(request, "请先把这项归档资料加入待整理。")
        return redirect("knowledge:document_detail", pk=document.pk)
    if request.method == "POST":
        form = DocumentOrganizeForm(
            request.POST,
            instance=document,
            family=member.family,
            created_by=member,
        )
        if form.is_valid():
            with transaction.atomic():
                document = form.save()
                document.knowledge_status = KnowledgeDocument.KNOWLEDGE_INCLUDED
                document.library_tier = KnowledgeDocument.LIBRARY_KNOWLEDGE
                document.curation_status = KnowledgeDocument.CURATION_CONFIRMED
                document.save(
                    update_fields=[
                        "knowledge_status",
                        "library_tier",
                        "curation_status",
                        "updated_at",
                    ]
                )
                record_curation_revision(
                    document,
                    changed_by=member,
                    change_type=KnowledgeCurationRevision.TYPE_MANUAL,
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
            messages.success(request, "正式整理结果已保存，并进入精选知识。")
            return redirect("knowledge:document_detail", pk=document.pk)
    else:
        form = DocumentOrganizeForm(
            instance=document,
            family=member.family,
            created_by=member,
        )
    return render(
        request,
        "knowledge/document_organize.html",
        {
            "document": document,
            "form": form,
            "existing_categories": form.existing_categories,
            "existing_tags": form.existing_tags,
            "is_reediting": document.library_tier == KnowledgeDocument.LIBRARY_KNOWLEDGE,
        },
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


def _visible_import_batches(member):
    queryset = KnowledgeImportBatch.objects.filter(family=member.family).select_related(
        "source",
        "requested_by",
    )
    if member.role == FamilyMember.ROLE_ADMIN:
        return queryset
    return queryset.filter(
        Q(requested_by=member) | Q(visibility=KnowledgeVisibility.FAMILY)
    )


@login_required
def imports(request):
    member = current_member(request)
    if member is None:
        return _membership_required_response(request)
    if request.method == "POST":
        if not _can_write(member):
            return HttpResponseForbidden("查看者不能创建导入批次。")
        form = KnowledgeImportUploadForm(request.POST, request.FILES)
        if form.is_valid():
            try:
                batch = create_uploaded_import_batch(
                    member=member,
                    source_name=form.cleaned_data["source_name"],
                    person_name=form.cleaned_data["person_name"],
                    category=form.cleaned_data["category"],
                    visibility=form.cleaned_data["visibility"],
                    uploaded_file=form.cleaned_data["package"],
                )
                queue_knowledge_job(
                    family=member.family,
                    source=batch.source,
                    requested_by=member,
                    job_type=KnowledgeJob.TYPE_PREVIEW_IMPORT,
                    parameters={"batch_id": batch.pk},
                )
                messages.success(
                    request,
                    "导入包已保存并进入格式检查；确认前不会建立知识文档。",
                )
                return redirect("knowledge:import_batch_detail", pk=batch.pk)
            except KnowledgeImportError as exc:
                form.add_error("package", str(exc))
    else:
        form = KnowledgeImportUploadForm()
    return render(
        request,
        "knowledge/imports.html",
        {
            "form": form,
            "batches": _visible_import_batches(member)[:25],
            "can_write": _can_write(member),
        },
    )


@login_required
def import_batch_detail(request, pk):
    member = current_member(request)
    if member is None:
        return _membership_required_response(request)
    batch = get_object_or_404(_visible_import_batches(member), pk=pk)
    may_view_items = (
        batch.requested_by_id == member.id
        or batch.visibility == KnowledgeVisibility.FAMILY
    )
    latest_job = batch.source.jobs.filter(parameters__batch_id=batch.pk).first()
    can_retry_import = batch.status == KnowledgeImportBatch.STATUS_PREVIEW_READY or (
        batch.status == KnowledgeImportBatch.STATUS_PARTIAL
        and batch.rolled_back_at is None
    )
    return render(
        request,
        "knowledge/import_batch_detail.html",
        {
            "batch": batch,
            "items": batch.items.all() if may_view_items else batch.items.none(),
            "items_redacted": not may_view_items,
            "latest_job": latest_job,
            "can_retry_import": can_retry_import,
            "can_manage": _can_write(member)
            and (
                batch.requested_by_id == member.id
                or member.role == FamilyMember.ROLE_ADMIN
            ),
        },
    )


def _queue_import_batch_job(request, batch, job_type):
    member = current_member(request)
    if (
        not _can_write(member)
        or (
            batch.requested_by_id != member.id
            and member.role != FamilyMember.ROLE_ADMIN
        )
    ):
        return HttpResponseForbidden("无权操作该导入批次。")
    job, created = queue_knowledge_job(
        family=batch.family,
        source=batch.source,
        requested_by=member,
        job_type=job_type,
        parameters={"batch_id": batch.pk},
    )
    messages.success(
        request,
        "任务已加入队列。"
        if created
        else "该来源已有同类型任务正在排队或运行。",
    )
    return redirect("knowledge:job_detail", pk=job.pk)


@login_required
@require_POST
def import_batch_confirm(request, pk):
    member = current_member(request)
    if member is None:
        return _membership_required_response(request)
    batch = get_object_or_404(_visible_import_batches(member), pk=pk)
    if batch.status not in {
        KnowledgeImportBatch.STATUS_PREVIEW_READY,
        KnowledgeImportBatch.STATUS_PARTIAL,
    } or (batch.status == KnowledgeImportBatch.STATUS_PARTIAL and batch.rolled_back_at):
        messages.error(request, "只有完成格式检查或部分失败的批次可以导入。")
        return redirect("knowledge:import_batch_detail", pk=batch.pk)
    if batch.error_count and request.POST.get("accept_valid_items") != "on":
        messages.error(request, "批次存在格式错误；请明确勾选只导入有效项目。")
        return redirect("knowledge:import_batch_detail", pk=batch.pk)
    return _queue_import_batch_job(request, batch, KnowledgeJob.TYPE_IMPORT_BATCH)


@login_required
@require_POST
def import_batch_rollback(request, pk):
    member = current_member(request)
    if member is None:
        return _membership_required_response(request)
    batch = get_object_or_404(_visible_import_batches(member), pk=pk)
    if batch.status not in {
        KnowledgeImportBatch.STATUS_COMPLETED,
        KnowledgeImportBatch.STATUS_PARTIAL,
    }:
        messages.error(request, "当前批次不能回滚。")
        return redirect("knowledge:import_batch_detail", pk=batch.pk)
    return _queue_import_batch_job(request, batch, KnowledgeJob.TYPE_ROLLBACK_IMPORT)


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
            "archive_count": source.documents.filter(
                knowledge_status__in=[
                    KnowledgeDocument.KNOWLEDGE_INCLUDED,
                    KnowledgeDocument.KNOWLEDGE_PENDING,
                ],
            ).count(),
            "pending_count": source.documents.filter(
                knowledge_status=KnowledgeDocument.KNOWLEDGE_PENDING,
            ).count(),
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
def source_history(request, pk):
    member = current_member(request)
    if member is None:
        return _membership_required_response(request)
    source = get_object_or_404(visible_sources(member), pk=pk)
    if source.kind not in {
        KnowledgeSource.KIND_HTML_IMPORT,
        KnowledgeSource.KIND_MARKDOWN_IMPORT,
    }:
        return redirect(reverse("knowledge:library") + f"?source_id={source.pk}")
    return _library_response(
        request,
        member,
        forced_source_id=source.pk,
        forced_collection="archive",
        page_title=f"{source.name} · 历史文章",
        page_description="此人物的历史文章保存在归档资料中；加入待整理并完成人工确认后，才会同时出现在精选知识。",
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
            update_fields = []
            if document.knowledge_status != desired_status:
                document.knowledge_status = desired_status
                update_fields.append("knowledge_status")
            if (
                document.curation_status != KnowledgeDocument.CURATION_CONFIRMED
                and document.library_tier != KnowledgeDocument.LIBRARY_ARCHIVE
            ):
                document.library_tier = KnowledgeDocument.LIBRARY_ARCHIVE
                update_fields.append("library_tier")
            if update_fields:
                document.save(update_fields=[*update_fields, "updated_at"])
                updated_documents += 1
    if source.kind == KnowledgeSource.KIND_ONENOTE:
        success_message = (
            f"来源设置已更新，并按分区规则更新了 {updated_documents} 篇已有文档。"
            if request.POST.get("apply_existing") == "on"
            else "来源设置已更新；分区规则只影响以后首次同步的页面。"
        )
    else:
        success_message = "来源设置已更新。"
    messages.success(request, success_message)
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
        if job.job_type == KnowledgeJob.TYPE_GENERATE_PROPOSALS:
            restore_ai_processing_documents(job)
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
    job, created = queue_knowledge_job(
        family=previous.family,
        source=previous.source,
        requested_by=member,
        job_type=previous.job_type,
        parameters=previous.parameters,
    )
    if created and job.job_type == KnowledgeJob.TYPE_GENERATE_PROPOSALS:
        job.total_count = len((job.parameters or {}).get("document_ids") or [])
        job.save(update_fields=["total_count", "updated_at"])
        mark_ai_processing_documents(job)
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
    was_curated = (
        document.knowledge_status == KnowledgeDocument.KNOWLEDGE_INCLUDED
        and document.library_tier == KnowledgeDocument.LIBRARY_KNOWLEDGE
    )
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
            tag_items = ensure_tags(
                document.family,
                final_value,
                created_by=member,
            )
            document.tags = [item.name for item, _ in tag_items]
            final_value = document.tags
            changed_field = "tags"
        elif proposal.proposal_type == KnowledgeProposal.TYPE_CATEGORY:
            category, _ = ensure_category(
                document.family,
                final_value,
                created_by=member,
            )
            document.category = category.name if category else ""
            final_value = document.category
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
        run_has_accepted = document.proposals.filter(
            run=proposal.run,
            status=KnowledgeProposal.STATUS_ACCEPTED,
        ).exists()
        has_formal_result = bool(
            document.confirmed_summary or document.category or document.tags
        )
        if was_curated or run_has_accepted or has_formal_result:
            document.curation_status = KnowledgeDocument.CURATION_CONFIRMED
            document.knowledge_status = KnowledgeDocument.KNOWLEDGE_INCLUDED
            document.library_tier = KnowledgeDocument.LIBRARY_KNOWLEDGE
        else:
            document.curation_status = KnowledgeDocument.CURATION_NORMALIZED
            document.knowledge_status = KnowledgeDocument.KNOWLEDGE_PENDING
            document.library_tier = KnowledgeDocument.LIBRARY_ARCHIVE
        document.save(
            update_fields=[
                "curation_status",
                "knowledge_status",
                "library_tier",
                "updated_at",
            ]
        )
        if run_has_accepted:
            record_curation_revision(
                document,
                changed_by=member,
                change_type=KnowledgeCurationRevision.TYPE_AI_CONFIRMED,
                proposal_run=proposal.run,
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
