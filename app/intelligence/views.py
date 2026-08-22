from datetime import timedelta
from collections import defaultdict

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Case, Count, IntegerField, Prefetch, Q, When
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
    EventMergeConfirmForm,
    IntelligenceEventForm,
    IntelligenceSourceForm,
    IntelligenceSubjectForm,
    ManualEventForm,
)
from .models import (
    CollectionRun,
    EventAnalysis,
    EventKnowledgeArchive,
    EventMergeRecord,
    EventMergeSuggestion,
    EventUserState,
    IntelligenceDigest,
    IntelligenceDigestItem,
    IntelligenceEvent,
    IntelligenceSource,
    IntelligenceSubject,
    SourceItem,
    SubjectFollow,
    SubjectKnowledgeIdentity,
)
from .ai_enrichment import (
    IntelligenceAiError,
    analyze_event,
    intelligence_provider_policy,
    provider_is_configured,
    text_ai_providers,
)
from .digest import (
    MAX_BATCH_ANALYSES,
    IntelligenceDigestError,
    analyze_digest_candidates,
    generate_daily_digest,
    pending_analysis_candidates,
)
from .event_merging import (
    AUTO_MERGE_ENABLED,
    EventMergeError,
    MERGE_POLICY_VERSION,
    merge_events,
    refresh_family_merge_suggestions,
    reject_merge_suggestion,
    split_merged_event,
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
TRIAGE_RECOMMENDATION_FEED = "feed"
TRIAGE_RECOMMENDATION_REVIEW = "review"
TRIAGE_RECOMMENDATION_NOISE = "noise"


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
        merged_into__isnull=True,
    )
    if not include_nonpublic:
        queryset = queryset.filter(
            review_status__in=VISIBLE_EVENT_STATUSES,
            selection_status__in=PUBLIC_SELECTION_STATUSES,
        )
    return queryset.select_related(
        "primary_source_item__source",
    ).prefetch_related(
        "subjects",
        "evidence_links__source_item__source",
        Prefetch(
            "analyses",
            queryset=EventAnalysis.objects.filter(
                is_current=True,
                status=EventAnalysis.STATUS_SUCCESS,
            ).select_related("provider"),
            to_attr="_current_ai_analyses",
        ),
    )


def _pending_review_events(member):
    return IntelligenceEvent.objects.filter(
        family=member.family,
        channel=IntelligenceEvent.CHANNEL_PEOPLE,
        merged_into__isnull=True,
        review_status=IntelligenceEvent.REVIEW_PENDING,
        selection_status=IntelligenceEvent.SELECTION_REVIEW,
    )


def _triage_recommendation(event):
    """Return a deterministic, non-publishing recommendation for a candidate."""
    source = event.primary_source_item.source if event.primary_source_item_id else None
    source_tier = source.source_tier if source else IntelligenceSource.TIER_D
    if (
        source_tier in {IntelligenceSource.TIER_A, IntelligenceSource.TIER_B}
        and event.importance_score >= 50
        and event.confidence_score >= 70
    ):
        return {
            "code": TRIAGE_RECOMMENDATION_FEED,
            "label": "建议保留到全部动态",
            "reason": "一手或直接来源，且重要性与置信度达到首轮批量保留门槛；不会进入今日精选。",
        }
    if (
        source_tier == IntelligenceSource.TIER_D
        or (event.importance_score < 45 and event.confidence_score < 70)
    ):
        return {
            "code": TRIAGE_RECOMMENDATION_NOISE,
            "label": "建议移入噪音箱",
            "reason": "来源或综合分数不足，保留原始证据但不占用日常信息流。",
        }
    return {
        "code": TRIAGE_RECOMMENDATION_REVIEW,
        "label": "需要单项确认",
        "reason": "媒体或分数处于中间区间，请先核查标题、短摘录和原文链接。",
    }


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
                merged_into__isnull=True,
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
def digest_workbench(request):
    member = _current_member(request)
    can_admin = _is_family_admin(request)
    digest = (
        IntelligenceDigest.objects.filter(family=member.family)
        .select_related("generated_by")
        .prefetch_related("items__event", "items__analysis")
        .first()
    )
    digest_items = list(digest.items.all()) if digest else []
    item_groups = {
        IntelligenceDigestItem.BUCKET_IMPORTANT: [
            item for item in digest_items if item.bucket == IntelligenceDigestItem.BUCKET_IMPORTANT
        ],
        IntelligenceDigestItem.BUCKET_FOLLOW_UP: [
            item for item in digest_items if item.bucket == IntelligenceDigestItem.BUCKET_FOLLOW_UP
        ],
        IntelligenceDigestItem.BUCKET_REVIEW: [
            item for item in digest_items
            if can_admin and item.bucket == IntelligenceDigestItem.BUCKET_REVIEW
        ],
    }
    provider_options = []
    for provider in text_ai_providers():
        configured = provider_is_configured(provider)
        policy = None
        if configured:
            try:
                policy = intelligence_provider_policy(provider)
            except IntelligenceAiError:
                configured = False
        provider_options.append(
            {
                "provider": provider,
                "configured": configured,
                "max_cost": policy["max_estimated_usd"] if policy else None,
                "batch_max_cost": (
                    policy["max_estimated_usd"] * MAX_BATCH_ANALYSES if policy else None
                ),
                "max_output_tokens": policy["max_output_tokens"] if policy else None,
            }
        )
    candidates = pending_analysis_candidates(member.family) if can_admin else []
    analyzed_candidate_count = sum(bool(event.current_ai_analysis) for event in candidates)
    return render(
        request,
        "intelligence/digest_workbench.html",
        {
            "digest": digest,
            "important_items": item_groups[IntelligenceDigestItem.BUCKET_IMPORTANT],
            "follow_up_items": item_groups[IntelligenceDigestItem.BUCKET_FOLLOW_UP],
            "review_items": item_groups[IntelligenceDigestItem.BUCKET_REVIEW],
            "candidates": candidates,
            "analyzed_candidate_count": analyzed_candidate_count,
            "provider_options": provider_options,
            "configured_provider_count": sum(item["configured"] for item in provider_options),
            "max_batch_analyses": MAX_BATCH_ANALYSES,
            "can_admin": can_admin,
        },
    )


@login_required
@require_POST
def digest_analyze_batch(request):
    member = _current_member(request)
    if not _is_family_admin(request):
        return _admin_required_response()
    provider_id = request.POST.get("provider_id", "").strip()
    if provider_id and not provider_id.isdigit():
        messages.error(request, "AI 服务商参数不正确，请刷新页面后重试。")
        return redirect("intelligence:digest_workbench")
    event_ids = [value for value in request.POST.getlist("event_ids") if value.isdigit()]
    try:
        run = analyze_digest_candidates(
            family=member.family,
            member=member,
            user=request.user,
            event_ids=event_ids,
            provider_id=int(provider_id) if provider_id else None,
        )
    except (IntelligenceAiError, IntelligenceDigestError) as exc:
        messages.error(request, str(exc))
    else:
        summary = (
            f"AI 整理完成：成功 {run.classified_count}，新调用 {run.updated_count}，"
            f"复用 {run.ignored_count}，失败 {run.failed_count}。"
        )
        if run.status == CollectionRun.STATUS_SUCCESS:
            messages.success(request, summary)
        else:
            messages.warning(request, summary + " 请查看运行记录中的安全错误摘要。")
    return redirect("intelligence:digest_workbench")


@login_required
@require_POST
def digest_generate(request):
    member = _current_member(request)
    if not _is_family_admin(request):
        return _admin_required_response()
    try:
        digest, changed, _run = generate_daily_digest(
            family=member.family,
            user=request.user,
        )
    except IntelligenceDigestError as exc:
        messages.error(request, str(exc))
    else:
        if changed:
            messages.success(
                request,
                f"已生成 {digest.digest_date:%Y-%m-%d} 情报简报；AI 摘要与证据采用当时快照。",
            )
        else:
            messages.info(request, "候选、AI 分析和分层没有变化，已复用现有简报。")
    return redirect("intelligence:digest_workbench")


@login_required
def triage_review(request):
    member = _current_member(request)
    if not _is_family_admin(request):
        return _admin_required_response()
    query = request.GET.get("q", "").strip()
    event_type = request.GET.get("type", "").strip()
    subject_id = request.GET.get("subject", "").strip()
    source_tier = request.GET.get("source_tier", "").strip()

    events = _pending_review_events(member).select_related(
        "primary_source_item__source",
    ).prefetch_related("subjects")
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
    valid_tiers = {value for value, _label in IntelligenceSource.TIER_CHOICES}
    if source_tier in valid_tiers:
        events = events.filter(primary_source_item__source__source_tier=source_tier)
    else:
        source_tier = ""

    tier_rank = Case(
        When(primary_source_item__source__source_tier=IntelligenceSource.TIER_A, then=0),
        When(primary_source_item__source__source_tier=IntelligenceSource.TIER_B, then=1),
        When(primary_source_item__source__source_tier=IntelligenceSource.TIER_C, then=2),
        When(primary_source_item__source__source_tier=IntelligenceSource.TIER_D, then=3),
        default=4,
        output_field=IntegerField(),
    )
    candidates = list(
        events.distinct()
        .annotate(_triage_tier_rank=tier_rank)
        .order_by("_triage_tier_rank", "-importance_score", "-confidence_score", "-occurred_at", "-pk")
    )
    recommendation_counts = defaultdict(int)
    for event in candidates:
        event.triage_recommendation = _triage_recommendation(event)
        recommendation_counts[event.triage_recommendation["code"]] += 1
    page_obj = Paginator(candidates, 20).get_page(request.GET.get("page"))
    return render(
        request,
        "intelligence/triage_review.html",
        {
            "page_obj": page_obj,
            "query": query,
            "selected_type": event_type,
            "selected_subject": subject_id,
            "selected_source_tier": source_tier,
            "event_types": IntelligenceEvent.TYPE_CHOICES,
            "source_tiers": IntelligenceSource.TIER_CHOICES,
            "subjects": IntelligenceSubject.objects.filter(is_active=True),
            "recommendation_counts": recommendation_counts,
            "return_url": request.get_full_path(),
            "can_admin": True,
        },
    )


@login_required
@require_POST
def triage_batch_apply(request):
    member = _current_member(request)
    if not _is_family_admin(request):
        return _admin_required_response()
    action = request.POST.get("action", "").strip()
    if action not in {TRIAGE_RECOMMENDATION_FEED, TRIAGE_RECOMMENDATION_NOISE}:
        messages.error(request, "请选择“保留到全部动态”或“移入噪音箱”。")
        return _redirect_next_or(request, "intelligence:triage_review")
    selected_ids = list(dict.fromkeys(
        value for value in request.POST.getlist("event_ids") if value.isdigit()
    ))[:50]
    events = list(
        _pending_review_events(member)
        .filter(pk__in=selected_ids)
        .order_by("pk")
    )
    if not events:
        messages.info(request, "没有选择仍处于待复核状态的候选。")
        return _redirect_next_or(request, "intelligence:triage_review")
    for event in events:
        if action == TRIAGE_RECOMMENDATION_FEED:
            event.review_status = IntelligenceEvent.REVIEW_REVIEWED
            # 批量保留只进入全部动态，绝不因分数自动推入今日精选。
            event.selection_status = IntelligenceEvent.SELECTION_FEED
        else:
            event.review_status = IntelligenceEvent.REVIEW_IGNORED
            event.selection_status = IntelligenceEvent.SELECTION_NOISE
        stamp_actor(event, request.user)
        event.save(update_fields=["review_status", "selection_status", "updated_by", "updated_at"])
    if action == TRIAGE_RECOMMENDATION_FEED:
        messages.success(request, f"已将 {len(events)} 条候选保留到全部动态；没有自动加入今日精选。")
    else:
        messages.success(request, f"已将 {len(events)} 条候选移入噪音箱；原始证据仍被保留。")
    return _redirect_next_or(request, "intelligence:triage_review")


@login_required
def event_detail(request, pk):
    member = _current_member(request)
    can_admin = _is_family_admin(request)
    if can_admin:
        event_queryset = IntelligenceEvent.objects.filter(family=member.family).select_related(
            "primary_source_item__source",
            "merged_into",
        ).prefetch_related(
            "subjects",
            "evidence_links__source_item__source",
            Prefetch(
                "analyses",
                queryset=EventAnalysis.objects.filter(
                    is_current=True,
                    status=EventAnalysis.STATUS_SUCCESS,
                ).select_related("provider"),
                to_attr="_current_ai_analyses",
            ),
        )
    else:
        event = get_object_or_404(
            IntelligenceEvent.objects.select_related("merged_into"),
            family=member.family,
            pk=pk,
        )
        if event.merged_into_id:
            return redirect("intelligence:event_detail", pk=event.merged_into_id)
        event_queryset = _family_events(member)
    event = get_object_or_404(event_queryset, pk=pk)
    is_public_event = (
        event.review_status in VISIBLE_EVENT_STATUSES
        and event.selection_status in PUBLIC_SELECTION_STATUSES
    )
    can_interact = (
        member.role != FamilyMember.ROLE_VIEWER
        and is_public_event
        and not event.merged_into_id
    )
    state = EventUserState.objects.filter(member=member, event=event).first()
    archive_link = (
        EventKnowledgeArchive.objects.filter(event=event)
        .select_related("document", "document__owner")
        .first()
    )
    analysis_history = list(
        event.analyses.select_related("provider", "created_by").order_by("-created_at", "-pk")[:5]
    )
    providers = [
        {"provider": provider, "configured": provider_is_configured(provider)}
        for provider in text_ai_providers()
    ] if can_admin else []
    merge_suggestions = []
    active_merges = []
    if can_admin:
        merge_suggestions = list(
            EventMergeSuggestion.objects.filter(
                Q(left_event=event) | Q(right_event=event),
                family=member.family,
                status=EventMergeSuggestion.STATUS_PENDING,
            ).select_related(
                "left_event",
                "right_event",
                "recommended_event",
                "recommended_primary_source__source",
            )[:8]
        )
        active_merges = list(
            EventMergeRecord.objects.filter(
                canonical_event=event,
                status=EventMergeRecord.STATUS_ACTIVE,
            ).select_related("duplicate_event", "merged_by")
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
                    or can_admin
                )
            ),
            "can_admin": can_admin,
            "current_analysis": event.current_ai_analysis,
            "analysis_history": analysis_history,
            "text_ai_providers": providers,
            "configured_text_ai_count": sum(item["configured"] for item in providers),
            "merge_suggestions": merge_suggestions,
            "active_merges": active_merges,
        },
    )


@login_required
@require_POST
def event_analyze(request, pk):
    member = _current_member(request)
    if not _is_family_admin(request):
        return _admin_required_response()
    event = get_object_or_404(IntelligenceEvent, family=member.family, pk=pk)
    provider_id = request.POST.get("provider_id", "").strip()
    if provider_id and not provider_id.isdigit():
        messages.error(request, "AI 服务商参数不正确，请刷新页面后重试。")
        return redirect("intelligence:event_detail", pk=event.pk)
    try:
        analysis, created = analyze_event(
            event,
            member=member,
            user=request.user,
            provider_id=int(provider_id) if provider_id else None,
            force=request.POST.get("force") == "1",
        )
    except IntelligenceAiError as exc:
        messages.error(request, str(exc))
    else:
        if created:
            messages.success(
                request,
                "AI 结构化分析已完成；摘要和特征已绑定来源，最终分层由代码重新计算。",
            )
        else:
            messages.info(request, "来源和模型版本没有变化，已复用现有 AI 分析，未重复调用。")
    return redirect("intelligence:event_detail", pk=event.pk)


@login_required
def merge_review(request):
    member = _current_member(request)
    if not _is_family_admin(request):
        return _admin_required_response()
    suggestions = list(
        EventMergeSuggestion.objects.filter(
            family=member.family,
            status=EventMergeSuggestion.STATUS_PENDING,
        ).select_related(
            "left_event__primary_source_item__source",
            "right_event__primary_source_item__source",
            "recommended_event",
            "recommended_primary_source__source",
        ).prefetch_related(
            "left_event__subjects",
            "right_event__subjects",
        )
    )
    batch_suggestions = [item for item in suggestions if item.decision_band == item.BAND_BATCH]
    review_suggestions = [item for item in suggestions if item.decision_band == item.BAND_REVIEW]
    return render(
        request,
        "intelligence/merge_review.html",
        {
            "batch_suggestions": batch_suggestions,
            "review_suggestions": review_suggestions,
            "pending_count": len(suggestions),
            "active_merge_count": EventMergeRecord.objects.filter(
                family=member.family,
                status=EventMergeRecord.STATUS_ACTIVE,
            ).count(),
            "policy_version": MERGE_POLICY_VERSION,
            "auto_merge_enabled": AUTO_MERGE_ENABLED,
            "can_admin": True,
        },
    )


@login_required
@require_POST
def merge_suggestion_refresh(request):
    member = _current_member(request)
    if not _is_family_admin(request):
        return _admin_required_response()
    suggestions = refresh_family_merge_suggestions(member.family)
    messages.success(request, f"已重新计算同一事件建议，当前有 {suggestions.count()} 组候选。")
    return redirect("intelligence:merge_review")


@login_required
def merge_suggestion_confirm(request, pk):
    member = _current_member(request)
    if not _is_family_admin(request):
        return _admin_required_response()
    suggestion = get_object_or_404(
        EventMergeSuggestion.objects.select_related(
            "left_event__primary_source_item__source",
            "right_event__primary_source_item__source",
            "recommended_event",
            "recommended_primary_source__source",
        ).prefetch_related(
            "left_event__subjects",
            "right_event__subjects",
            "left_event__evidence_links__source_item__source",
            "right_event__evidence_links__source_item__source",
        ),
        family=member.family,
        status=EventMergeSuggestion.STATUS_PENDING,
        pk=pk,
    )
    if request.method == "POST":
        form = EventMergeConfirmForm(request.POST, suggestion=suggestion)
        if form.is_valid():
            canonical = form.cleaned_data["canonical_event"]
            duplicate = (
                suggestion.right_event
                if canonical.pk == suggestion.left_event_id
                else suggestion.left_event
            )
            try:
                merge_events(
                    canonical_event=canonical,
                    duplicate_event=duplicate,
                    primary_source_item=form.cleaned_data["primary_source_item"],
                    user=request.user,
                    suggestion=suggestion,
                )
            except EventMergeError as exc:
                form.add_error(None, str(exc))
            else:
                messages.success(
                    request,
                    "两条事件已聚合；全部原始来源和历史版本均已保留，可在事件详情中拆分。",
                )
                return redirect("intelligence:event_detail", pk=canonical.pk)
    else:
        form = EventMergeConfirmForm(suggestion=suggestion)
    return render(
        request,
        "intelligence/merge_confirm.html",
        {
            "suggestion": suggestion,
            "compared_events": [suggestion.left_event, suggestion.right_event],
            "form": form,
            "can_admin": True,
        },
    )


@login_required
@require_POST
def merge_suggestion_batch_accept(request):
    member = _current_member(request)
    if not _is_family_admin(request):
        return _admin_required_response()
    selected_ids = [value for value in request.POST.getlist("suggestion_ids") if value.isdigit()][:50]
    suggestions = list(
        EventMergeSuggestion.objects.filter(
            family=member.family,
            status=EventMergeSuggestion.STATUS_PENDING,
            decision_band=EventMergeSuggestion.BAND_BATCH,
            requires_individual_review=False,
            pk__in=selected_ids,
        ).select_related(
            "left_event",
            "right_event",
            "recommended_event",
            "recommended_primary_source",
        )
    )
    merged_count = 0
    errors = []
    for suggestion in suggestions:
        canonical = suggestion.recommended_event
        duplicate = (
            suggestion.right_event
            if canonical.pk == suggestion.left_event_id
            else suggestion.left_event
        )
        try:
            merge_events(
                canonical_event=canonical,
                duplicate_event=duplicate,
                primary_source_item=suggestion.recommended_primary_source,
                user=request.user,
                suggestion=suggestion,
            )
        except EventMergeError as exc:
            errors.append(f"建议 #{suggestion.pk}：{exc}")
        else:
            merged_count += 1
    if merged_count:
        messages.success(request, f"已批量聚合 {merged_count} 组高置信度事件。")
    if errors:
        messages.warning(request, "；".join(errors[:5]))
    if not merged_count and not errors:
        messages.info(request, "没有选择仍可批量处理的建议。")
    return redirect("intelligence:merge_review")


@login_required
@require_POST
def merge_suggestion_reject(request, pk):
    member = _current_member(request)
    if not _is_family_admin(request):
        return _admin_required_response()
    suggestion = get_object_or_404(
        EventMergeSuggestion,
        family=member.family,
        pk=pk,
    )
    try:
        reject_merge_suggestion(suggestion=suggestion, user=request.user)
    except EventMergeError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "已标记为不同事件；当前策略版本不会再次提示这一对。")
    return _redirect_next_or(request, "intelligence:merge_review")


@login_required
@require_POST
def merge_record_split(request, pk):
    member = _current_member(request)
    if not _is_family_admin(request):
        return _admin_required_response()
    record = get_object_or_404(
        EventMergeRecord,
        family=member.family,
        status=EventMergeRecord.STATUS_ACTIVE,
        pk=pk,
    )
    canonical_id = record.canonical_event_id
    try:
        split_merged_event(merge_record=record, user=request.user)
    except EventMergeError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "事件已拆分；两条事件及各自原始证据重新独立显示。")
    return redirect("intelligence:event_detail", pk=canonical_id)


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
            updated_event.analyses.filter(is_current=True).update(is_current=False)
            rescore_event(updated_event)
            messages.success(request, "情报事件已更新；如曾有 AI 分析，旧版本已保留但不再作为当前结果。")
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
    current_analyses = EventAnalysis.objects.filter(
        event__family=member.family,
        status=EventAnalysis.STATUS_SUCCESS,
        is_current=True,
    )
    providers = text_ai_providers()
    configured_provider_count = sum(provider_is_configured(provider) for provider in providers)
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
        {"number": 3, "name": "规则门控与结构化整理", "count": source_items.filter(processed_at__isnull=False).count(), "state": "code", "note": "确定性门控先过滤噪音；M3 对管理员明确选择的候选执行一次版本化 AI 分析。"},
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
            "current_analysis_count": current_analyses.count(),
            "failed_analysis_count": EventAnalysis.objects.filter(
                event__family=member.family,
                status=EventAnalysis.STATUS_FAILED,
            ).count(),
            "text_ai_provider_count": len(providers),
            "configured_text_ai_count": configured_provider_count,
            "can_admin": True,
        },
    )
