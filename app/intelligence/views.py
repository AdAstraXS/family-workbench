from datetime import timedelta
from collections import defaultdict

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from family_core.audit import stamp_actor
from family_core.models import FamilyMember
from knowledge.models import KnowledgeDocument
from knowledge.permissions import accessible_search_entries

from .forms import (
    IntelligenceEventForm,
    IntelligenceSourceForm,
    IntelligenceSubjectForm,
    ManualEventForm,
)
from .models import (
    CollectionRun,
    EventKnowledgeArchive,
    EventUserState,
    IntelligenceEvent,
    IntelligenceSource,
    IntelligenceSubject,
    SourceItem,
    SubjectFollow,
    SubjectKnowledgeIdentity,
)
from .services import (
    IntelligenceArchiveError,
    IntelligenceArchivePermissionError,
    archive_event_to_knowledge,
    create_manual_event,
)
from .scoring import POLICY_VERSION, rescore_event
from .collection import SUPPORTED_ADAPTERS, collect_intelligence_sources


VISIBLE_EVENT_STATUSES = (
    IntelligenceEvent.REVIEW_PUBLISHED,
    IntelligenceEvent.REVIEW_REVIEWED,
)
PUBLIC_SELECTION_STATUSES = (
    IntelligenceEvent.SELECTION_SELECTED,
    IntelligenceEvent.SELECTION_FEED,
)


def _current_member(request):
    return getattr(request, "family_member", None)


def _is_family_admin(request):
    member = _current_member(request)
    return bool(member and (request.user.is_superuser or member.role == FamilyMember.ROLE_ADMIN))


def _admin_required_response():
    return HttpResponseForbidden("只有家庭管理员可以管理关注主题、信源和情报事件。")


def _redirect_next_or(request, viewname, **kwargs):
    next_url = request.POST.get("next", "")
    if next_url and url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return redirect(next_url)
    return redirect(viewname, **kwargs)


def _family_events(member, *, include_nonpublic=False):
    queryset = IntelligenceEvent.objects.filter(
        family=member.family,
        channel=IntelligenceEvent.CHANNEL_PEOPLE,
    )
    if not include_nonpublic:
        queryset = queryset.filter(
            review_status__in=VISIBLE_EVENT_STATUSES,
            selection_status__in=PUBLIC_SELECTION_STATUSES,
        )
    return queryset.select_related(
        "primary_source_item__source",
    ).prefetch_related("subjects", "evidence_links__source_item__source")


def _attach_user_states(events, member):
    event_list = list(events)
    states = {
        state.event_id: state
        for state in EventUserState.objects.filter(
            member=member,
            event_id__in=[event.pk for event in event_list],
        )
    }
    for event in event_list:
        event.member_state = states.get(event.pk)
    return event_list


@login_required
def index(request):
    member = _current_member(request)
    today = timezone.localdate()
    followed_ids = SubjectFollow.objects.filter(
        family=member.family,
        is_active=True,
        is_muted=False,
    ).values_list("subject_id", flat=True)
    events = _family_events(member).filter(subjects__in=followed_ids).distinct()
    important_events = _attach_user_states(
        events.filter(
            selection_status=IntelligenceEvent.SELECTION_SELECTED,
            occurred_at__date__gte=today - timedelta(days=1),
        )
        .order_by("-importance_score", "-occurred_at")[:8],
        member,
    )
    latest_events = _attach_user_states(events.order_by("-occurred_at")[:12], member)
    followed_subjects = (
        IntelligenceSubject.objects.filter(
            family_follows__family=member.family,
            family_follows__is_active=True,
            is_active=True,
        )
        .annotate(event_count=Count("intelligence_events", filter=Q(intelligence_events__family=member.family), distinct=True))
        .order_by("-family_follows__priority", "-importance_level", "display_name")[:10]
    )
    return render(
        request,
        "intelligence/index.html",
        {
            "important_events": important_events,
            "latest_events": latest_events,
            "followed_subjects": followed_subjects,
            "followed_count": SubjectFollow.objects.filter(family=member.family, is_active=True).count(),
            "unread_count": events.exclude(user_states__member=member, user_states__read_at__isnull=False).distinct().count(),
            "pending_count": IntelligenceEvent.objects.filter(
                family=member.family,
                selection_status=IntelligenceEvent.SELECTION_REVIEW,
            ).count(),
            "can_admin": _is_family_admin(request),
        },
    )


@login_required
def event_list(request):
    member = _current_member(request)
    query = request.GET.get("q", "").strip()
    event_type = request.GET.get("type", "").strip()
    subject_id = request.GET.get("subject", "").strip()
    selection = request.GET.get("selection", "").strip()
    include_nonpublic = _is_family_admin(request) and selection in {
        IntelligenceEvent.SELECTION_REVIEW,
        IntelligenceEvent.SELECTION_NOISE,
    }
    events = _family_events(member, include_nonpublic=include_nonpublic)
    if query:
        events = events.filter(
            Q(title__icontains=query)
            | Q(summary__icontains=query)
            | Q(why_it_matters__icontains=query)
        )
    valid_types = {value for value, _label in IntelligenceEvent.TYPE_CHOICES}
    if event_type in valid_types:
        events = events.filter(event_type=event_type)
    else:
        event_type = ""
    if subject_id.isdigit():
        events = events.filter(subjects__pk=subject_id)
    else:
        subject_id = ""
    if selection:
        valid_selections = {value for value, _label in IntelligenceEvent.SELECTION_CHOICES}
        if selection in valid_selections and (
            selection in PUBLIC_SELECTION_STATUSES or _is_family_admin(request)
        ):
            events = events.filter(selection_status=selection)
        else:
            selection = ""
    events = events.distinct()
    page_obj = Paginator(events, 20).get_page(request.GET.get("page"))
    page_obj.object_list = _attach_user_states(page_obj.object_list, member)
    return render(
        request,
        "intelligence/event_list.html",
        {
            "page_obj": page_obj,
            "query": query,
            "selected_type": event_type,
            "selected_subject": subject_id,
            "selected_selection": selection,
            "event_types": IntelligenceEvent.TYPE_CHOICES,
            "selection_statuses": IntelligenceEvent.SELECTION_CHOICES,
            "subjects": IntelligenceSubject.objects.filter(is_active=True),
            "can_admin": _is_family_admin(request),
        },
    )


@login_required
def event_detail(request, pk):
    member = _current_member(request)
    event = get_object_or_404(
        _family_events(member, include_nonpublic=_is_family_admin(request)),
        pk=pk,
    )
    is_public_event = (
        event.review_status in VISIBLE_EVENT_STATUSES
        and event.selection_status in PUBLIC_SELECTION_STATUSES
    )
    can_interact = (
        member.role != FamilyMember.ROLE_VIEWER
        and is_public_event
    )
    state = EventUserState.objects.filter(member=member, event=event).first()
    archive_link = (
        EventKnowledgeArchive.objects.filter(event=event)
        .select_related("document", "document__owner")
        .first()
    )
    return render(
        request,
        "intelligence/event_detail.html",
        {
            "event": event,
            "member_state": state,
            "archive_link": archive_link,
            "can_interact": can_interact,
            "can_archive": can_interact,
            "can_upgrade_archive": bool(
                can_interact
                and archive_link
                and (
                    archive_link.document.owner_id == member.pk
                    or _is_family_admin(request)
                )
            ),
            "can_admin": _is_family_admin(request),
        },
    )


@login_required
@require_POST
def event_archive(request, pk):
    member = _current_member(request)
    event = get_object_or_404(_family_events(member), pk=pk)
    mode = request.POST.get("mode", EventKnowledgeArchive.MODE_ARCHIVE)
    if mode not in dict(EventKnowledgeArchive.MODE_CHOICES):
        messages.error(request, "归档方式不正确，请刷新页面后重试。")
        return redirect("intelligence:event_detail", pk=event.pk)
    try:
        link, created, upgraded = archive_event_to_knowledge(
            event=event,
            member=member,
            user=request.user,
            add_to_pending=mode == EventKnowledgeArchive.MODE_ORGANIZE,
        )
    except IntelligenceArchivePermissionError as exc:
        return HttpResponseForbidden(str(exc))
    except IntelligenceArchiveError as exc:
        messages.error(request, str(exc))
        return redirect("intelligence:event_detail", pk=event.pk)
    if created and mode == EventKnowledgeArchive.MODE_ORGANIZE:
        messages.success(request, "已保存到知识中心，并加入待整理。")
    elif created:
        messages.success(request, "已保存到知识中心的归档资料。")
    elif upgraded:
        messages.success(request, "已将归档资料加入待整理；原始情报快照没有改写。")
    else:
        messages.info(request, "这条情报已经保存到知识中心，没有重复创建。")
    return redirect("intelligence:event_detail", pk=event.pk)


@login_required
def manual_event_create(request):
    member = _current_member(request)
    if not _is_family_admin(request):
        return _admin_required_response()
    if request.method == "POST":
        form = ManualEventForm(request.POST)
        if form.is_valid():
            event, created = create_manual_event(
                cleaned_data=form.cleaned_data,
                member=member,
                user=request.user,
            )
            if created:
                messages.success(request, "人工情报事件已创建，并已绑定原始证据。")
            else:
                messages.info(request, "系统识别到相同事件，已复用原记录并更新时间。")
            return redirect("intelligence:event_detail", pk=event.pk)
    else:
        form = ManualEventForm(
            initial={
                "occurred_at": timezone.localtime().strftime("%Y-%m-%dT%H:%M"),
                "occurred_precision": IntelligenceEvent.PRECISION_EXACT,
                "change_type": IntelligenceEvent.CHANGE_UNKNOWN,
                "review_status": IntelligenceEvent.REVIEW_PUBLISHED,
                "source_tier": IntelligenceSource.TIER_C,
                "source_group": IntelligenceSource.GROUP_MEDIA,
                "evidence_type": "fact",
            }
        )
    return render(
        request,
        "intelligence/manual_event_form.html",
        {"form": form, "title": "人工录入关键人物动态"},
    )


@login_required
def event_edit(request, pk):
    member = _current_member(request)
    if not _is_family_admin(request):
        return _admin_required_response()
    event = get_object_or_404(IntelligenceEvent, family=member.family, pk=pk)
    if request.method == "POST":
        form = IntelligenceEventForm(request.POST, instance=event)
        if form.is_valid():
            updated_event = form.save(commit=False)
            stamp_actor(updated_event, request.user)
            updated_event.save()
            rescore_event(updated_event)
            messages.success(request, "情报事件已更新。")
            return redirect("intelligence:event_detail", pk=event.pk)
    else:
        form = IntelligenceEventForm(instance=event)
    return render(
        request,
        "intelligence/model_form.html",
        {"form": form, "title": "编辑情报事件", "cancel_url": event_detail_url(event)},
    )


def event_detail_url(event):
    from django.urls import reverse

    return reverse("intelligence:event_detail", kwargs={"pk": event.pk})


@login_required
@require_POST
def event_ignore(request, pk):
    member = _current_member(request)
    if not _is_family_admin(request):
        return _admin_required_response()
    event = get_object_or_404(IntelligenceEvent, family=member.family, pk=pk)
    event.review_status = IntelligenceEvent.REVIEW_IGNORED
    stamp_actor(event, request.user)
    event.save(update_fields=["review_status", "updated_by", "updated_at"])
    rescore_event(event)
    messages.success(request, "该事件已标记为忽略，不再出现在普通信息流中。")
    return redirect("intelligence:event_list")


@login_required
@require_POST
def event_toggle_bookmark(request, pk):
    member = _current_member(request)
    event = get_object_or_404(_family_events(member), pk=pk)
    state, _ = EventUserState.objects.get_or_create(member=member, event=event)
    state.bookmarked_at = None if state.bookmarked_at else timezone.now()
    state.save(update_fields=["bookmarked_at", "updated_at"])
    return _redirect_next_or(request, "intelligence:event_detail", pk=event.pk)


@login_required
@require_POST
def event_mark_read(request, pk):
    member = _current_member(request)
    event = get_object_or_404(_family_events(member), pk=pk)
    state, _ = EventUserState.objects.get_or_create(member=member, event=event)
    if not state.read_at:
        state.read_at = timezone.now()
        state.save(update_fields=["read_at", "updated_at"])
    return _redirect_next_or(request, "intelligence:event_detail", pk=event.pk)


@login_required
def subject_list(request):
    member = _current_member(request)
    query = request.GET.get("q", "").strip()
    category = request.GET.get("category", "").strip()
    subjects = IntelligenceSubject.objects.filter(is_active=True).annotate(
        source_count=Count("sources", filter=Q(sources__is_active=True), distinct=True),
        event_count=Count(
            "intelligence_events",
            filter=Q(intelligence_events__family=member.family, intelligence_events__review_status__in=VISIBLE_EVENT_STATUSES),
            distinct=True,
        ),
    )
    if query:
        subjects = subjects.filter(Q(display_name__icontains=query) | Q(canonical_name__icontains=query))
    valid_categories = {value for value, _label in IntelligenceSubject.CATEGORY_CHOICES}
    if category in valid_categories:
        subjects = subjects.filter(category=category)
    else:
        category = ""
    follows = {
        follow.subject_id: follow
        for follow in SubjectFollow.objects.filter(family=member.family)
    }
    subject_list_value = list(subjects)
    for subject in subject_list_value:
        subject.family_follow = follows.get(subject.pk)
    return render(
        request,
        "intelligence/subject_list.html",
        {
            "subjects": subject_list_value,
            "query": query,
            "selected_category": category,
            "categories": IntelligenceSubject.CATEGORY_CHOICES,
            "can_admin": _is_family_admin(request),
        },
    )


@login_required
def subject_detail(request, slug):
    member = _current_member(request)
    subject = get_object_or_404(
        IntelligenceSubject.objects.prefetch_related("sources", "primary_sources", "outgoing_relations__to_subject", "incoming_relations__from_subject"),
        slug=slug,
    )
    events = _attach_user_states(
        _family_events(member).filter(subjects=subject).distinct()[:50],
        member,
    )
    follow = SubjectFollow.objects.filter(family=member.family, subject=subject).first()
    outgoing_relations = list(subject.outgoing_relations.all())
    incoming_relations = list(subject.incoming_relations.all())
    knowledge_author_names = list(
        SubjectKnowledgeIdentity.objects.filter(
            family=member.family,
            subject=subject,
            is_active=True,
        ).values_list("author_name", flat=True)
    )
    knowledge_count = 0
    if knowledge_author_names:
        knowledge_count = accessible_search_entries(member).filter(
            author_name__in=knowledge_author_names,
            knowledge_status__in=[
                KnowledgeDocument.KNOWLEDGE_INCLUDED,
                KnowledgeDocument.KNOWLEDGE_PENDING,
            ],
        ).count()
    return render(
        request,
        "intelligence/subject_detail.html",
        {
            "subject": subject,
            "events": events,
            "follow": follow,
            "outgoing_relations": outgoing_relations,
            "incoming_relations": incoming_relations,
            "knowledge_author_names": knowledge_author_names,
            "knowledge_count": knowledge_count,
            "knowledge_people_url": reverse("knowledge:people")
            + f"?subject={subject.slug}",
            "can_admin": _is_family_admin(request),
        },
    )


@login_required
def subject_create(request):
    member = _current_member(request)
    if not _is_family_admin(request):
        return _admin_required_response()
    if request.method == "POST":
        form = IntelligenceSubjectForm(request.POST, family=member.family)
        if form.is_valid():
            subject = form.save()
            form.save_knowledge_identities(subject=subject, user=request.user)
            messages.success(request, "关注主题已创建。")
            return redirect("intelligence:subject_detail", slug=subject.slug)
    else:
        form = IntelligenceSubjectForm(family=member.family)
    return render(request, "intelligence/model_form.html", {"form": form, "title": "新增关注主题"})


@login_required
def subject_edit(request, slug):
    member = _current_member(request)
    if not _is_family_admin(request):
        return _admin_required_response()
    subject = get_object_or_404(IntelligenceSubject, slug=slug)
    if request.method == "POST":
        form = IntelligenceSubjectForm(
            request.POST,
            instance=subject,
            family=member.family,
        )
        if form.is_valid():
            subject = form.save()
            form.save_knowledge_identities(subject=subject, user=request.user)
            messages.success(request, "关注主题已更新。")
            return redirect("intelligence:subject_detail", slug=subject.slug)
    else:
        form = IntelligenceSubjectForm(instance=subject, family=member.family)
    return render(
        request,
        "intelligence/model_form.html",
        {"form": form, "title": "编辑关注主题", "cancel_url": subject_detail_url(subject)},
    )


def subject_detail_url(subject):
    from django.urls import reverse

    return reverse("intelligence:subject_detail", kwargs={"slug": subject.slug})


@login_required
@require_POST
def subject_toggle_follow(request, slug):
    member = _current_member(request)
    subject = get_object_or_404(IntelligenceSubject, slug=slug, is_active=True)
    follow, created = SubjectFollow.objects.get_or_create(
        family=member.family,
        subject=subject,
        defaults={"added_by": request.user, "is_active": True},
    )
    if not created:
        follow.is_active = not follow.is_active
        follow.added_by = request.user
        follow.save(update_fields=["is_active", "added_by", "updated_at"])
    messages.success(request, "已关注该对象。" if follow.is_active else "已取消关注该对象。")
    return _redirect_next_or(request, "intelligence:subject_detail", slug=subject.slug)


@login_required
def source_create(request):
    if not _is_family_admin(request):
        return _admin_required_response()
    subject_id = request.GET.get("subject", "")
    initial = {
        "adapter_key": IntelligenceSource.ADAPTER_MANUAL,
        "source_type": IntelligenceSource.TYPE_MANUAL,
        "source_group": IntelligenceSource.GROUP_OTHER,
    }
    if subject_id.isdigit():
        initial["subject"] = subject_id
        initial["topics"] = [subject_id]
    if request.method == "POST":
        form = IntelligenceSourceForm(request.POST)
        if form.is_valid():
            source = form.save()
            messages.success(request, "信源已创建。")
            return redirect("intelligence:source_list")
    else:
        form = IntelligenceSourceForm(initial=initial)
    return render(request, "intelligence/model_form.html", {"form": form, "title": "新增信源"})


@login_required
def source_edit(request, pk):
    if not _is_family_admin(request):
        return _admin_required_response()
    source = get_object_or_404(IntelligenceSource.objects.select_related("subject"), pk=pk)
    if request.method == "POST":
        form = IntelligenceSourceForm(request.POST, instance=source)
        if form.is_valid():
            form.save()
            messages.success(request, "信源已更新。")
            return redirect("intelligence:source_list")
    else:
        form = IntelligenceSourceForm(instance=source)
    return render(
        request,
        "intelligence/model_form.html",
        {"form": form, "title": "编辑信源", "cancel_url": reverse_url("intelligence:source_list")},
    )


def reverse_url(viewname, **kwargs):
    from django.urls import reverse

    return reverse(viewname, kwargs=kwargs or None)


@login_required
def operations(request):
    member = _current_member(request)
    if not _is_family_admin(request):
        return _admin_required_response()
    sources = IntelligenceSource.objects.prefetch_related("topics").order_by(
        "-is_active", "source_tier", "source_group", "name"
    )
    runs = CollectionRun.objects.filter(
        Q(family=member.family) | Q(family__isnull=True)
    ).select_related("created_by").prefetch_related("source_results__source")[:30]
    recent_items = SourceItem.objects.select_related("source").prefetch_related("matched_subjects")[:30]
    automatic_sources = sources.filter(adapter_key__in=SUPPORTED_ADAPTERS)
    return render(
        request,
        "intelligence/operations.html",
        {
            "sources": sources,
            "runs": runs,
            "pending_items": IntelligenceEvent.objects.filter(
                family=member.family,
                selection_status=IntelligenceEvent.SELECTION_REVIEW,
            ).count(),
            "recent_items": recent_items,
            "automatic_source_count": automatic_sources.count(),
            "active_automatic_source_count": automatic_sources.filter(is_active=True).count(),
            "disabled_automatic_source_count": automatic_sources.filter(is_active=False).count(),
            "due_source_count": sum(source.is_due for source in sources),
            "can_admin": True,
        },
    )


@login_required
@require_POST
def collect_sources_now(request):
    member = _current_member(request)
    if not _is_family_admin(request):
        return _admin_required_response()
    source_ids = [
        int(value)
        for value in request.POST.getlist("source_id")
        if value.isdigit()
    ]
    force = request.POST.get("force") == "1"
    run = collect_intelligence_sources(
        source_ids=source_ids or None,
        due_only=not force,
        max_items=50,
        created_by=request.user,
        family=member.family,
    )
    summary = (
        f"采集完成：发现 {run.discovered_count}，新增 {run.created_count}，"
        f"重复 {run.ignored_count}，噪音 {run.noise_count}，待复核 {run.review_count}。"
    )
    if run.status == CollectionRun.STATUS_SUCCESS:
        messages.success(request, summary)
    elif run.status == CollectionRun.STATUS_PARTIAL:
        messages.warning(request, summary + " 部分信源失败，请查看运行记录。")
    else:
        messages.error(request, summary + " 本次信源均未成功，请查看错误摘要。")
    return redirect("intelligence:operations")


@login_required
def source_list(request):
    sources = list(
        IntelligenceSource.objects.prefetch_related("topics").order_by(
            "source_tier", "source_group", "name"
        )
    )
    query = request.GET.get("q", "").strip()
    if query:
        sources = [
            source
            for source in sources
            if query.casefold() in source.name.casefold()
            or any(query.casefold() in topic.display_name.casefold() for topic in source.topics.all())
        ]

    tier_descriptions = {
        IntelligenceSource.TIER_A: "官方站点、官方账号、监管披露和正式文件",
        IntelligenceSource.TIER_B: "直接采访、演讲、播客与人物本人内容",
        IntelligenceSource.TIER_C: "可信媒体、研究机构与专业二手报道",
        IntelligenceSource.TIER_D: "用于发现线索，不直接作为高置信度结论",
    }
    grouped = defaultdict(lambda: defaultdict(list))
    for source in sources:
        grouped[source.source_tier][source.source_group].append(source)
    tier_sections = []
    for tier, tier_label in IntelligenceSource.TIER_CHOICES:
        groups = [
            {
                "code": group_code,
                "label": group_label,
                "sources": grouped[tier].get(group_code, []),
            }
            for group_code, group_label in IntelligenceSource.GROUP_CHOICES
            if grouped[tier].get(group_code)
        ]
        tier_sections.append(
            {
                "tier": tier,
                "label": tier_label,
                "description": tier_descriptions[tier],
                "groups": groups,
                "count": sum(len(group["sources"]) for group in groups),
            }
        )

    platform_counts = [
        {
            "code": source_type,
            "label": label,
            "count": sum(1 for source in sources if source.source_type == source_type),
        }
        for source_type, label in IntelligenceSource.TYPE_CHOICES
        if any(source.source_type == source_type for source in sources)
    ]
    return render(
        request,
        "intelligence/source_list.html",
        {
            "sources": sources,
            "tier_sections": tier_sections,
            "platform_counts": platform_counts,
            "query": query,
            "active_count": sum(source.is_active for source in sources),
            "abnormal_count": sum(source.health_status == "error" for source in sources),
            "due_count": sum(source.is_due for source in sources),
            "manual_count": sum(source.adapter_key == IntelligenceSource.ADAPTER_MANUAL for source in sources),
            "automatic_count": sum(
                source.is_active and source.adapter_key in SUPPORTED_ADAPTERS for source in sources
            ),
            "registered_automatic_count": sum(source.adapter_key in SUPPORTED_ADAPTERS for source in sources),
            "hidden_count": sum(not source.is_active for source in sources),
            "can_admin": _is_family_admin(request),
            "automatic_collection_enabled": True,
        },
    )


@login_required
def pipeline(request):
    member = _current_member(request)
    if not _is_family_admin(request):
        return _admin_required_response()
    family_events = IntelligenceEvent.objects.filter(family=member.family)
    source_items = SourceItem.objects.all()
    processed_statuses = {
        SourceItem.STATUS_NORMALIZED,
        SourceItem.STATUS_CLASSIFIED,
        SourceItem.STATUS_SCORED,
        SourceItem.STATUS_CLUSTERED,
        SourceItem.STATUS_ANALYZED,
        SourceItem.STATUS_PUBLISHED,
        SourceItem.STATUS_NOISE,
    }
    stage_cards = [
        {"number": 1, "name": "抓取并保留来源记录", "count": source_items.count(), "state": "collect", "note": "RSS 公开订阅元数据已启用；YouTube 适配器已登记但默认停用，不下载视频或音频。"},
        {"number": 2, "name": "标准化与确定性去重", "count": source_items.filter(processing_status__in=processed_statuses).count(), "state": "code", "note": "平台 ID、链接和内容指纹；不调用 AI。"},
        {"number": 3, "name": "规则分类与相关性门控", "count": source_items.filter(processed_at__isnull=False).count(), "state": "code", "note": "官方源使用主题匹配，媒体源要求标题直提关注对象；文本模型尚未接入。"},
        {"number": 4, "name": "代码评分与事件聚合", "count": family_events.exclude(scoring_breakdown={}).count(), "state": "code", "note": f"策略 {POLICY_VERSION}，相同输入得到稳定结果。"},
        {"number": 5, "name": "分层展示", "count": family_events.filter(selection_status=IntelligenceEvent.SELECTION_SELECTED).count(), "state": "decision", "note": "精选、全部动态、待复核和噪音箱。"},
    ]
    return render(
        request,
        "intelligence/pipeline.html",
        {
            "stage_cards": stage_cards,
            "selection_counts": [
                {
                    "code": code,
                    "label": label,
                    "count": family_events.filter(selection_status=code).count(),
                }
                for code, label in IntelligenceEvent.SELECTION_CHOICES
            ],
            "failed_count": source_items.filter(processing_status=SourceItem.STATUS_FAILED).count(),
            "recent_runs": CollectionRun.objects.filter(Q(family=member.family) | Q(family__isnull=True))[:20],
            "policy_version": POLICY_VERSION,
            "automatic_collection_enabled": True,
            "can_admin": True,
        },
    )
