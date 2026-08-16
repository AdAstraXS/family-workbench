import hashlib
import re

from django.db import transaction
from django.utils import timezone

from family_core.audit import stamp_actor

from .models import (
    CollectionRun,
    EventEvidence,
    EventSubject,
    IntelligenceEvent,
    IntelligenceSource,
    SourceItem,
)
from .scoring import POLICY_VERSION, SOURCE_QUALITY_SCORES, calculate_event_scores


def _normalized_text(value):
    return re.sub(r"\s+", " ", (value or "").strip().casefold())


def _fingerprint(*parts):
    payload = "|".join(_normalized_text(part) for part in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@transaction.atomic
def create_manual_event(*, cleaned_data, member, user):
    subject = cleaned_data["subject"]
    source, _ = IntelligenceSource.objects.get_or_create(
        subject=subject,
        name=cleaned_data["source_name"].strip(),
        defaults={
            "source_type": IntelligenceSource.TYPE_MANUAL,
            "adapter_key": "manual",
            "source_group": cleaned_data["source_group"],
            "source_tier": cleaned_data["source_tier"],
            "is_active": True,
        },
    )
    source.topics.add(subject)

    source_item_hash = _fingerprint(
        cleaned_data["source_url"],
        cleaned_data["source_title"],
        cleaned_data["occurred_at"].isoformat(),
    )
    source_item = SourceItem.objects.filter(
        source=source,
        content_hash=source_item_hash,
    ).first()
    if source_item is None:
        source_item = SourceItem(
            source=source,
            canonical_url=cleaned_data["source_url"],
            title=cleaned_data["source_title"],
            author_name=cleaned_data["source_author"],
            published_at=cleaned_data["occurred_at"],
            excerpt=cleaned_data["evidence_excerpt"],
            content_hash=source_item_hash,
            content_depth=SourceItem.DEPTH_MANUAL,
            classification_labels=[cleaned_data["event_type"], subject.category],
            relevance_score=cleaned_data["relevance_score"],
            processing_status=SourceItem.STATUS_PUBLISHED,
            processing_reason="人工录入，已完成确定性评分。",
            processed_at=timezone.now(),
        )
        stamp_actor(source_item, user)
        source_item.save()
    source_item.matched_subjects.add(subject)

    cluster_key = _fingerprint(
        str(member.family_id),
        str(subject.pk),
        cleaned_data["title"],
        cleaned_data["occurred_at"].date().isoformat(),
        cleaned_data["source_url"],
    )
    event = IntelligenceEvent.objects.filter(
        family=member.family,
        channel=IntelligenceEvent.CHANNEL_PEOPLE,
        cluster_key=cluster_key,
    ).first()
    created = event is None
    if event is None:
        scoring = calculate_event_scores(
            relevance=cleaned_data["relevance_score"],
            impact=cleaned_data["impact_score"],
            novelty=cleaned_data["novelty_score"],
            actionability=cleaned_data["actionability_score"],
            timeliness=cleaned_data["timeliness_score"],
            source_tier=cleaned_data["source_tier"],
            has_url=bool(cleaned_data["source_url"]),
            has_excerpt=bool(cleaned_data["evidence_excerpt"]),
            source_count=1,
            extraction_confidence=100,
            change_type=cleaned_data["change_type"],
            review_status=cleaned_data["review_status"],
        )
        event = IntelligenceEvent(
            family=member.family,
            channel=IntelligenceEvent.CHANNEL_PEOPLE,
            event_type=cleaned_data["event_type"],
            title=cleaned_data["title"],
            occurred_at=cleaned_data["occurred_at"],
            occurred_precision=cleaned_data["occurred_precision"],
            summary=cleaned_data["summary"],
            why_it_matters=cleaned_data["why_it_matters"],
            relevance_score=cleaned_data["relevance_score"],
            impact_score=cleaned_data["impact_score"],
            novelty_score=cleaned_data["novelty_score"],
            actionability_score=cleaned_data["actionability_score"],
            timeliness_score=cleaned_data["timeliness_score"],
            importance_score=scoring.importance_score,
            confidence_score=scoring.confidence_score,
            change_type=cleaned_data["change_type"],
            review_status=cleaned_data["review_status"],
            selection_status=scoring.selection_status,
            scoring_policy_version=POLICY_VERSION,
            scoring_breakdown=scoring.breakdown,
            score_origin=IntelligenceEvent.SCORE_ORIGIN_MANUAL,
            cluster_key=cluster_key,
            primary_source_item=source_item,
        )
        stamp_actor(event, user)
        event.save()
    else:
        event.last_seen_at = timezone.now()
        stamp_actor(event, user)
        event.save(update_fields=["last_seen_at", "updated_by", "updated_at"])

    EventSubject.objects.get_or_create(
        event=event,
        subject=subject,
        role=EventSubject.ROLE_SUBJECT,
        defaults={"confidence_score": 100, "is_primary": True},
    )
    EventEvidence.objects.get_or_create(
        event=event,
        source_item=source_item,
        defaults={
            "evidence_type": cleaned_data["evidence_type"],
            "excerpt": cleaned_data["evidence_excerpt"],
            "source_quality_score": SOURCE_QUALITY_SCORES[cleaned_data["source_tier"]],
            "is_primary": True,
        },
    )
    CollectionRun.objects.create(
        family=member.family,
        run_kind=CollectionRun.KIND_MANUAL,
        status=CollectionRun.STATUS_SUCCESS,
        started_at=timezone.now(),
        finished_at=timezone.now(),
        discovered_count=1,
        created_count=1 if created else 0,
        updated_count=0 if created else 1,
        normalized_count=1,
        classified_count=1,
        noise_count=1 if event.selection_status == IntelligenceEvent.SELECTION_NOISE else 0,
        clustered_count=1,
        selected_count=1 if event.selection_status == IntelligenceEvent.SELECTION_SELECTED else 0,
        review_count=1 if event.selection_status == IntelligenceEvent.SELECTION_REVIEW else 0,
        parameters={"subject_id": subject.pk, "source_id": source.pk},
        created_by=user,
    )
    return event, created
