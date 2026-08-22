import re
from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .models import (
    EventEvidence,
    EventKnowledgeArchive,
    EventMergeRecord,
    EventMergeSuggestion,
    EventSubject,
    EventUserState,
    IntelligenceEvent,
    IntelligenceSource,
    SourceItem,
)
from .scoring import rescore_event


MERGE_POLICY_VERSION = "event-merge-v1"
SUGGESTION_THRESHOLD = 48
BATCH_THRESHOLD = 75
FUTURE_AUTO_THRESHOLD = 90
AUTO_MERGE_ENABLED = False

STOP_WORDS = {
    "the", "and", "for", "with", "from", "into", "official", "video", "new",
    "a", "an", "of", "to", "in", "on", "at", "by", "is", "are", "as", "its",
}
TIER_RANK = {
    IntelligenceSource.TIER_A: 4,
    IntelligenceSource.TIER_B: 3,
    IntelligenceSource.TIER_C: 2,
    IntelligenceSource.TIER_D: 1,
}
GROUP_RANK = {
    IntelligenceSource.GROUP_REGULATORY: 7,
    IntelligenceSource.GROUP_OFFICIAL: 6,
    IntelligenceSource.GROUP_EXPERT: 5,
    IntelligenceSource.GROUP_INSTITUTION: 4,
    IntelligenceSource.GROUP_MEDIA: 3,
    IntelligenceSource.GROUP_SOCIAL: 2,
    IntelligenceSource.GROUP_OTHER: 1,
}
DEPTH_RANK = {
    SourceItem.DEPTH_MANUAL: 5,
    SourceItem.DEPTH_TRANSCRIPT: 4,
    SourceItem.DEPTH_OFFICIAL_ARTICLE: 3,
    SourceItem.DEPTH_DESCRIPTION: 2,
    SourceItem.DEPTH_TITLE: 1,
}


class EventMergeError(RuntimeError):
    pass


def _normalized_text(value):
    return re.sub(r"\s+", " ", str(value or "").strip().casefold())


def _tokens(value):
    normalized = _normalized_text(value)
    latin_tokens = {
        token for token in re.findall(r"[a-z0-9]+", normalized)
        if len(token) > 1 and token not in STOP_WORDS
    }
    cjk_tokens = set()
    for block in re.findall(r"[\u3400-\u9fff]+", normalized):
        if len(block) == 1:
            cjk_tokens.add(block)
        else:
            cjk_tokens.update(block[index:index + 2] for index in range(len(block) - 1))
    return latin_tokens | cjk_tokens


def _similarity(left, right):
    left_tokens = _tokens(left)
    right_tokens = _tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def _number_tokens(value):
    return set(re.findall(r"(?<![a-z])\$?\d+(?:[.,]\d+)?%?", _normalized_text(value)))


def source_rank(item):
    source = item.source
    return (
        TIER_RANK.get(source.source_tier, 0),
        GROUP_RANK.get(source.source_group, 0),
        DEPTH_RANK.get(item.content_depth, 0),
        int(bool(item.excerpt)),
        source.transport_weight,
        -(item.pk or 0),
    )


def recommended_primary_source(*events):
    items = {}
    for event in events:
        for link in event.evidence_links.select_related("source_item__source"):
            items[link.source_item_id] = link.source_item
    return max(items.values(), key=source_rank, default=None)


def _event_rank(event):
    archived = EventKnowledgeArchive.objects.filter(event=event).exists()
    reviewed = event.review_status in {
        IntelligenceEvent.REVIEW_PUBLISHED,
        IntelligenceEvent.REVIEW_AI_PUBLISHED,
        IntelligenceEvent.REVIEW_REVIEWED,
    }
    primary = event.primary_source_item
    return (
        int(archived),
        int(reviewed),
        source_rank(primary) if primary else (0, 0, 0, 0, 0, 0),
        event.evidence_links.count(),
        -event.pk,
    )


def recommended_canonical_event(left, right):
    return max((left, right), key=_event_rank)


def _protected_reasons(event):
    reasons = []
    if event.review_status in {
        IntelligenceEvent.REVIEW_PUBLISHED,
        IntelligenceEvent.REVIEW_AI_PUBLISHED,
        IntelligenceEvent.REVIEW_REVIEWED,
    }:
        reasons.append("事件已经发布或人工复核")
    if EventKnowledgeArchive.objects.filter(event=event).exists():
        reasons.append("事件已经保存到知识中心")
    if event.user_states.filter(bookmarked_at__isnull=False).exists():
        reasons.append("至少一名成员已经收藏")
    return reasons


def evaluate_event_pair(left, right):
    if left.family_id != right.family_id or left.pk == right.pk:
        return None
    if left.merged_into_id or right.merged_into_id:
        return None
    left_subjects = set(left.subjects.values_list("pk", flat=True))
    right_subjects = set(right.subjects.values_list("pk", flat=True))
    shared_subjects = left_subjects & right_subjects
    if not shared_subjects:
        return None
    time_delta = abs(left.occurred_at - right.occurred_at)
    if time_delta > timedelta(days=3):
        return None

    left_links = list(left.evidence_links.select_related("source_item__source"))
    right_links = list(right.evidence_links.select_related("source_item__source"))
    left_sources = {link.source_item.source_id for link in left_links}
    right_sources = {link.source_item.source_id for link in right_links}
    if not left_sources or not right_sources or left_sources == right_sources:
        return None
    title_pairs = [(left.title, right.title)]
    title_pairs.extend(
        (left_link.source_item.title, right_link.source_item.title)
        for left_link in left_links
        for right_link in right_links
    )
    title_similarity = max((_similarity(a, b) for a, b in title_pairs), default=0.0)
    summary_similarity = _similarity(left.display_summary, right.display_summary)
    numbers_left = _number_tokens(" ".join([left.title, left.display_summary]))
    numbers_right = _number_tokens(" ".join([right.title, right.display_summary]))
    shared_numbers = sorted(numbers_left & numbers_right)
    time_score = max(0, 10 - round(time_delta.total_seconds() / 86400 * 3))
    score = round(
        title_similarity * 55
        + summary_similarity * 15
        + min(2, len(shared_subjects)) * 5
        + time_score
        + (5 if left.event_type == right.event_type else 0)
        + (10 if shared_numbers else 0)
    )
    score = max(0, min(100, score))
    if score < SUGGESTION_THRESHOLD:
        return None

    protected_reasons = [
        *[f"#{left.pk}：{reason}" for reason in _protected_reasons(left)],
        *[f"#{right.pk}：{reason}" for reason in _protected_reasons(right)],
    ]
    strong_anchor = title_similarity >= 0.8 or (
        title_similarity >= 0.65 and bool(shared_numbers)
    )
    auto_merge_eligible = (
        score >= FUTURE_AUTO_THRESHOLD
        and strong_anchor
        and not protected_reasons
    )
    requires_individual_review = bool(protected_reasons) or score < BATCH_THRESHOLD
    decision_band = (
        EventMergeSuggestion.BAND_REVIEW
        if requires_individual_review
        else EventMergeSuggestion.BAND_BATCH
    )
    recommended_event = recommended_canonical_event(left, right)
    primary_source = recommended_primary_source(left, right)
    return {
        "score": score,
        "decision_band": decision_band,
        "auto_merge_eligible": auto_merge_eligible,
        "requires_individual_review": requires_individual_review,
        "recommended_event": recommended_event,
        "recommended_primary_source": primary_source,
        "reason": {
            "title_similarity": round(title_similarity, 3),
            "summary_similarity": round(summary_similarity, 3),
            "shared_subject_ids": sorted(shared_subjects),
            "shared_numbers": shared_numbers,
            "time_delta_hours": round(time_delta.total_seconds() / 3600, 1),
            "same_event_type": left.event_type == right.event_type,
            "cross_source": True,
            "protected_reasons": protected_reasons,
            "auto_merge_enabled": AUTO_MERGE_ENABLED,
            "policy_explanation": (
                "高置信度建议可批量确认；语义自动聚合在真实误合并率验收前保持关闭。"
            ),
        },
    }


def _save_suggestion(left, right, evaluation):
    left, right = sorted((left, right), key=lambda event: event.pk)
    suggestion, created = EventMergeSuggestion.objects.get_or_create(
        family=left.family,
        left_event=left,
        right_event=right,
        policy_version=MERGE_POLICY_VERSION,
        defaults={
            **evaluation,
            "status": EventMergeSuggestion.STATUS_PENDING,
        },
    )
    if not created and suggestion.status == EventMergeSuggestion.STATUS_PENDING:
        for field in (
            "score",
            "decision_band",
            "auto_merge_eligible",
            "requires_individual_review",
            "recommended_event",
            "recommended_primary_source",
            "reason",
        ):
            setattr(suggestion, field, evaluation[field])
        suggestion.save(update_fields=[
            "score",
            "decision_band",
            "auto_merge_eligible",
            "requires_individual_review",
            "recommended_event",
            "recommended_primary_source",
            "reason",
            "updated_at",
        ])
    return suggestion, created


def refresh_merge_suggestions_for_event(event):
    if event.merged_into_id:
        return []
    subject_ids = list(event.subjects.values_list("pk", flat=True))
    if not subject_ids:
        return []
    candidates = (
        IntelligenceEvent.objects.filter(
            family=event.family,
            merged_into__isnull=True,
            occurred_at__gte=event.occurred_at - timedelta(days=3),
            occurred_at__lte=event.occurred_at + timedelta(days=3),
            subjects__in=subject_ids,
        )
        .exclude(pk=event.pk)
        .select_related("primary_source_item__source")
        .prefetch_related("subjects", "evidence_links__source_item__source")
        .distinct()
    )
    suggestions = []
    for candidate in candidates:
        evaluation = evaluate_event_pair(event, candidate)
        if evaluation:
            suggestion, _created = _save_suggestion(event, candidate, evaluation)
            suggestions.append(suggestion)
    return suggestions


def refresh_family_merge_suggestions(family):
    events = list(
        IntelligenceEvent.objects.filter(
            family=family,
            merged_into__isnull=True,
        )
        .select_related("primary_source_item__source")
        .prefetch_related("subjects", "evidence_links__source_item__source")
        .order_by("occurred_at", "pk")
    )
    active_pair_ids = set()
    for index, left in enumerate(events):
        for right in events[index + 1:]:
            if right.occurred_at - left.occurred_at > timedelta(days=3):
                break
            evaluation = evaluate_event_pair(left, right)
            if not evaluation:
                continue
            suggestion, _created = _save_suggestion(left, right, evaluation)
            active_pair_ids.add(suggestion.pk)
    EventMergeSuggestion.objects.filter(
        family=family,
        policy_version=MERGE_POLICY_VERSION,
        status=EventMergeSuggestion.STATUS_PENDING,
    ).exclude(pk__in=active_pair_ids).update(
        status=EventMergeSuggestion.STATUS_STALE,
        updated_at=timezone.now(),
    )
    return EventMergeSuggestion.objects.filter(
        pk__in=active_pair_ids,
        status=EventMergeSuggestion.STATUS_PENDING,
    )


def _copy_user_states(canonical, duplicate):
    for state in duplicate.user_states.all():
        target, _created = EventUserState.objects.get_or_create(
            member=state.member,
            event=canonical,
        )
        changed = []
        if state.read_at and not target.read_at:
            target.read_at = state.read_at
            changed.append("read_at")
        if state.bookmarked_at and not target.bookmarked_at:
            target.bookmarked_at = state.bookmarked_at
            changed.append("bookmarked_at")
        if changed:
            target.save(update_fields=[*changed, "updated_at"])


@transaction.atomic
def merge_events(*, canonical_event, duplicate_event, primary_source_item, user, suggestion=None):
    locked = {
        event.pk: event
        for event in IntelligenceEvent.objects.select_for_update().filter(
            pk__in=[canonical_event.pk, duplicate_event.pk]
        )
    }
    canonical = locked.get(canonical_event.pk)
    duplicate = locked.get(duplicate_event.pk)
    if not canonical or not duplicate or canonical.pk == duplicate.pk:
        raise EventMergeError("请选择两条不同的有效事件。")
    if canonical.family_id != duplicate.family_id:
        raise EventMergeError("不能合并其他家庭的事件。")
    if canonical.merged_into_id or duplicate.merged_into_id:
        raise EventMergeError("其中一条事件已经被合并，请刷新后重试。")
    if EventKnowledgeArchive.objects.filter(event=duplicate).exists():
        raise EventMergeError("并入事件已经保存到知识中心，请改为保留该事件。")
    if suggestion:
        suggestion = EventMergeSuggestion.objects.select_for_update().get(pk=suggestion.pk)
        pair_ids = {suggestion.left_event_id, suggestion.right_event_id}
        if pair_ids != {canonical.pk, duplicate.pk} or suggestion.status != suggestion.STATUS_PENDING:
            raise EventMergeError("合并建议已变化，请刷新后重试。")

    canonical_links = {
        link.source_item_id: link
        for link in canonical.evidence_links.select_for_update().all()
    }
    duplicate_links = list(duplicate.evidence_links.select_for_update().all())
    union_source_ids = set(canonical_links) | {link.source_item_id for link in duplicate_links}
    if not primary_source_item or primary_source_item.pk not in union_source_ids:
        raise EventMergeError("主来源必须来自这两条事件的已保存证据。")
    snapshot = {
        "canonical_primary_source_id": canonical.primary_source_item_id,
        "canonical_first_seen_at": canonical.first_seen_at.isoformat(),
        "canonical_last_seen_at": canonical.last_seen_at.isoformat(),
        "canonical_primary_flags": {
            str(link.pk): link.is_primary for link in canonical_links.values()
        },
        "added_evidence_link_ids": [],
        "added_subject_link_ids": [],
    }
    for link in duplicate_links:
        if link.source_item_id in canonical_links:
            continue
        copied = EventEvidence.objects.create(
            event=canonical,
            source_item=link.source_item,
            evidence_type=link.evidence_type,
            excerpt=link.excerpt,
            claim_ref=link.claim_ref,
            source_quality_score=link.source_quality_score,
            is_primary=False,
        )
        snapshot["added_evidence_link_ids"].append(copied.pk)
    canonical_subject_keys = set(
        canonical.subject_links.values_list("subject_id", "role")
    )
    for link in duplicate.subject_links.all():
        if (link.subject_id, link.role) in canonical_subject_keys:
            continue
        copied = EventSubject.objects.create(
            event=canonical,
            subject=link.subject,
            role=link.role,
            confidence_score=link.confidence_score,
            is_primary=False,
        )
        snapshot["added_subject_link_ids"].append(copied.pk)

    canonical.evidence_links.update(is_primary=False)
    canonical.evidence_links.filter(source_item=primary_source_item).update(is_primary=True)
    canonical.primary_source_item = primary_source_item
    canonical.first_seen_at = min(canonical.first_seen_at, duplicate.first_seen_at)
    canonical.last_seen_at = max(canonical.last_seen_at, duplicate.last_seen_at)
    canonical.updated_by = user
    canonical.save(update_fields=[
        "primary_source_item",
        "first_seen_at",
        "last_seen_at",
        "updated_by",
        "updated_at",
    ])
    canonical.analyses.filter(is_current=True).update(is_current=False)
    duplicate.merged_into = canonical
    duplicate.updated_by = user
    duplicate.save(update_fields=["merged_into", "updated_by", "updated_at"])
    _copy_user_states(canonical, duplicate)
    record = EventMergeRecord.objects.create(
        family=canonical.family,
        canonical_event=canonical,
        duplicate_event=duplicate,
        suggestion=suggestion,
        snapshot=snapshot,
        merged_by=user,
    )
    if suggestion:
        suggestion.status = suggestion.STATUS_ACCEPTED
        suggestion.reviewed_by = user
        suggestion.reviewed_at = timezone.now()
        suggestion.save(update_fields=["status", "reviewed_by", "reviewed_at", "updated_at"])
    EventMergeSuggestion.objects.filter(
        Q(left_event=duplicate) | Q(right_event=duplicate),
        status=EventMergeSuggestion.STATUS_PENDING,
    ).exclude(pk=getattr(suggestion, "pk", None)).update(
        status=EventMergeSuggestion.STATUS_STALE,
        updated_at=timezone.now(),
    )
    rescore_event(canonical)
    return record


@transaction.atomic
def split_merged_event(*, merge_record, user):
    record = EventMergeRecord.objects.select_for_update().select_related(
        "canonical_event",
        "duplicate_event",
    ).get(pk=merge_record.pk)
    if record.status != record.STATUS_ACTIVE:
        raise EventMergeError("这次合并已经拆分。")
    canonical = IntelligenceEvent.objects.select_for_update().get(pk=record.canonical_event_id)
    duplicate = IntelligenceEvent.objects.select_for_update().get(pk=record.duplicate_event_id)
    if duplicate.merged_into_id != canonical.pk:
        raise EventMergeError("事件合并状态已经变化，请刷新后重试。")
    snapshot = record.snapshot or {}
    added_evidence_ids = snapshot.get("added_evidence_link_ids") or []
    added_subject_ids = snapshot.get("added_subject_link_ids") or []
    EventEvidence.objects.filter(event=canonical, pk__in=added_evidence_ids).delete()
    EventSubject.objects.filter(event=canonical, pk__in=added_subject_ids).delete()
    canonical.evidence_links.update(is_primary=False)
    primary_flags = snapshot.get("canonical_primary_flags") or {}
    for link_id, is_primary in primary_flags.items():
        EventEvidence.objects.filter(event=canonical, pk=link_id).update(is_primary=bool(is_primary))
    prior_primary_id = snapshot.get("canonical_primary_source_id")
    if prior_primary_id and canonical.evidence_links.filter(source_item_id=prior_primary_id).exists():
        canonical.primary_source_item_id = prior_primary_id
    else:
        replacement = recommended_primary_source(canonical)
        canonical.primary_source_item = replacement
        if replacement:
            canonical.evidence_links.filter(source_item=replacement).update(is_primary=True)
    first_seen = parse_datetime(snapshot.get("canonical_first_seen_at") or "")
    last_seen = parse_datetime(snapshot.get("canonical_last_seen_at") or "")
    if first_seen:
        canonical.first_seen_at = first_seen
    if last_seen:
        canonical.last_seen_at = last_seen
    canonical.updated_by = user
    canonical.save(update_fields=[
        "primary_source_item",
        "first_seen_at",
        "last_seen_at",
        "updated_by",
        "updated_at",
    ])
    canonical.analyses.filter(is_current=True).update(is_current=False)
    duplicate.merged_into = None
    duplicate.updated_by = user
    duplicate.save(update_fields=["merged_into", "updated_by", "updated_at"])
    record.status = record.STATUS_REVERTED
    record.reverted_by = user
    record.reverted_at = timezone.now()
    record.save(update_fields=["status", "reverted_by", "reverted_at", "updated_at"])
    if record.suggestion_id:
        EventMergeSuggestion.objects.filter(pk=record.suggestion_id).update(
            status=EventMergeSuggestion.STATUS_REJECTED,
            reviewed_by=user,
            reviewed_at=timezone.now(),
            updated_at=timezone.now(),
        )
    rescore_event(canonical)
    rescore_event(duplicate)
    return record


def reject_merge_suggestion(*, suggestion, user):
    updated = EventMergeSuggestion.objects.filter(
        pk=suggestion.pk,
        status=EventMergeSuggestion.STATUS_PENDING,
    ).update(
        status=EventMergeSuggestion.STATUS_REJECTED,
        reviewed_by=user,
        reviewed_at=timezone.now(),
        updated_at=timezone.now(),
    )
    if not updated:
        raise EventMergeError("这条建议已经处理，请刷新后重试。")
