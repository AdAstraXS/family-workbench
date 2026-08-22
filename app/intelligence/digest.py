import hashlib
import json
from collections import defaultdict
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Prefetch, Q
from django.utils import timezone

from .ai_enrichment import (
    IntelligenceAiError,
    analyze_event,
    intelligence_provider_policy,
    resolve_text_ai_provider,
)
from .models import (
    CollectionRun,
    EventAnalysis,
    IntelligenceDigest,
    IntelligenceDigestItem,
    IntelligenceEvent,
)


DIGEST_POLICY_VERSION = IntelligenceDigest.POLICY_VERSION
MAX_BATCH_ANALYSES = 5
MAX_IMPORTANT_ITEMS = 5
MAX_FOLLOW_UP_ITEMS = 7
MAX_REVIEW_ITEMS = 8


class IntelligenceDigestError(RuntimeError):
    pass


def _day_window(digest_date):
    current_tz = timezone.get_current_timezone()
    start = timezone.make_aware(datetime.combine(digest_date, time.min), current_tz)
    return start, start + timedelta(days=1)


def _current_analysis_prefetch():
    return Prefetch(
        "analyses",
        queryset=EventAnalysis.objects.filter(
            status=EventAnalysis.STATUS_SUCCESS,
            is_current=True,
        ).select_related("provider"),
        to_attr="_current_ai_analyses",
    )


def digest_candidates(family, digest_date=None):
    digest_date = digest_date or timezone.localdate()
    window_start, window_end = _day_window(digest_date)
    queryset = (
        IntelligenceEvent.objects.filter(
            family=family,
            channel=IntelligenceEvent.CHANNEL_PEOPLE,
            merged_into__isnull=True,
            occurred_at__gte=window_start,
            occurred_at__lt=window_end,
        )
        .filter(
            Q(
                review_status=IntelligenceEvent.REVIEW_PENDING,
                selection_status=IntelligenceEvent.SELECTION_REVIEW,
            )
            | Q(
                review_status__in=[
                    IntelligenceEvent.REVIEW_PUBLISHED,
                    IntelligenceEvent.REVIEW_REVIEWED,
                ],
                selection_status__in=[
                    IntelligenceEvent.SELECTION_SELECTED,
                    IntelligenceEvent.SELECTION_FEED,
                ],
            )
        )
        .select_related("primary_source_item__source")
        .prefetch_related("subjects", _current_analysis_prefetch())
        .distinct()
    )
    candidates = [event for event in queryset if event.current_ai_analysis]
    return candidates, window_start, window_end


def pending_analysis_candidates(family, digest_date=None):
    digest_date = digest_date or timezone.localdate()
    window_start, window_end = _day_window(digest_date)
    return list(
        IntelligenceEvent.objects.filter(
            family=family,
            channel=IntelligenceEvent.CHANNEL_PEOPLE,
            merged_into__isnull=True,
            review_status=IntelligenceEvent.REVIEW_PENDING,
            selection_status=IntelligenceEvent.SELECTION_REVIEW,
            occurred_at__gte=window_start,
            occurred_at__lt=window_end,
        )
        .select_related("primary_source_item__source")
        .prefetch_related("subjects", _current_analysis_prefetch())
        .order_by("-importance_score", "-confidence_score", "-occurred_at", "-pk")[:20]
    )


def _digest_bucket(event):
    if event.review_status == IntelligenceEvent.REVIEW_PENDING:
        return IntelligenceDigestItem.BUCKET_REVIEW
    if event.selection_status == IntelligenceEvent.SELECTION_SELECTED:
        return IntelligenceDigestItem.BUCKET_IMPORTANT
    return IntelligenceDigestItem.BUCKET_FOLLOW_UP


def _ranked_items(events):
    grouped = defaultdict(list)
    for event in events:
        grouped[_digest_bucket(event)].append(event)
    for bucket in grouped:
        grouped[bucket].sort(
            key=lambda event: (
                -event.importance_score,
                -event.confidence_score,
                -event.occurred_at.timestamp(),
                -event.pk,
            )
        )
    limits = {
        IntelligenceDigestItem.BUCKET_IMPORTANT: MAX_IMPORTANT_ITEMS,
        IntelligenceDigestItem.BUCKET_FOLLOW_UP: MAX_FOLLOW_UP_ITEMS,
        IntelligenceDigestItem.BUCKET_REVIEW: MAX_REVIEW_ITEMS,
    }
    return [
        (bucket, event)
        for bucket in [
            IntelligenceDigestItem.BUCKET_IMPORTANT,
            IntelligenceDigestItem.BUCKET_FOLLOW_UP,
            IntelligenceDigestItem.BUCKET_REVIEW,
        ]
        for event in grouped[bucket][: limits[bucket]]
    ]


def _selection_reason(event, bucket):
    if bucket == IntelligenceDigestItem.BUCKET_IMPORTANT:
        return (
            f"已复核并进入今日精选；重要性 {event.importance_score}，"
            f"置信度 {event.confidence_score}。"
        )
    if bucket == IntelligenceDigestItem.BUCKET_FOLLOW_UP:
        return (
            f"已复核并保留到全部动态；重要性 {event.importance_score}，"
            f"置信度 {event.confidence_score}。"
        )
    return (
        f"AI 已完成结构化整理，但仍处于待复核；重要性 {event.importance_score}，"
        f"置信度 {event.confidence_score}。该组仅管理员可见。"
    )


def _fingerprint(ranked_items):
    payload = [
        {
            "event_id": event.pk,
            "analysis_id": event.current_ai_analysis.pk,
            "analysis_input": event.current_ai_analysis.input_fingerprint,
            "bucket": bucket,
            "importance": event.importance_score,
            "confidence": event.confidence_score,
            "review": event.review_status,
            "selection": event.selection_status,
        }
        for bucket, event in ranked_items
    ]
    encoded = json.dumps(
        {"policy": DIGEST_POLICY_VERSION, "items": payload},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _item_values(event, bucket, position):
    analysis = event.current_ai_analysis
    result = analysis.result_json or {}
    source_item = event.primary_source_item
    return {
        "event": event,
        "analysis": analysis,
        "bucket": bucket,
        "position": position,
        "selection_reason": _selection_reason(event, bucket),
        "title_snapshot": event.title,
        "summary_snapshot": result.get("summary") or event.summary,
        "why_it_matters_snapshot": result.get("why_it_matters") or event.why_it_matters,
        "subject_names": [subject.display_name for subject in event.subjects.all()],
        "source_name": source_item.source.name if source_item else "",
        "source_url": source_item.canonical_url if source_item else "",
        "occurred_at": event.occurred_at,
        "importance_score": event.importance_score,
        "confidence_score": event.confidence_score,
        "evidence_refs": result.get("summary_evidence_refs") or [],
        "model_name_snapshot": analysis.model_name or (analysis.provider.name if analysis.provider else ""),
        "tokens_used_snapshot": analysis.tokens_used or 0,
        "cost_estimate_snapshot": analysis.cost_estimate or Decimal("0"),
    }


def generate_daily_digest(*, family, user, digest_date=None):
    digest_date = digest_date or timezone.localdate()
    events, window_start, window_end = digest_candidates(family, digest_date)
    ranked_items = _ranked_items(events)
    run = CollectionRun.objects.create(
        family=family,
        run_kind=CollectionRun.KIND_DIGEST,
        status=CollectionRun.STATUS_RUNNING,
        parameters={
            "digest_date": digest_date.isoformat(),
            "policy_version": DIGEST_POLICY_VERSION,
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
        },
        created_by=user,
    )
    if not ranked_items:
        run.status = CollectionRun.STATUS_FAILED
        run.error_summary = "当天没有已完成 AI 整理且符合简报边界的事件。"
        run.finished_at = timezone.now()
        run.save(update_fields=["status", "error_summary", "finished_at"])
        raise IntelligenceDigestError(run.error_summary)

    fingerprint = _fingerprint(ranked_items)
    analyses = [event.current_ai_analysis for _bucket, event in ranked_items]
    provider_names = sorted(
        {
            analysis.model_name or analysis.provider.name
            for analysis in analyses
            if analysis.model_name or analysis.provider
        }
    )
    tokens_used = sum(analysis.tokens_used or 0 for analysis in analyses)
    cost_estimate = sum(
        (analysis.cost_estimate or Decimal("0")) for analysis in analyses
    ).quantize(Decimal("0.000001"))

    with transaction.atomic():
        digest = IntelligenceDigest.objects.select_for_update().filter(
            family=family,
            digest_date=digest_date,
        ).first()
        if digest and digest.input_fingerprint == fingerprint:
            changed = False
        else:
            defaults = {
                "title": f"{digest_date:%Y-%m-%d} AI 情报简报",
                "window_start": window_start,
                "window_end": window_end,
                "policy_version": DIGEST_POLICY_VERSION,
                "input_fingerprint": fingerprint,
                "provider_names": provider_names,
                "analysis_count": len(analyses),
                "tokens_used": tokens_used,
                "cost_estimate": cost_estimate,
                "generated_by": user,
                "generated_at": timezone.now(),
            }
            if digest is None:
                digest = IntelligenceDigest.objects.create(
                    family=family,
                    digest_date=digest_date,
                    **defaults,
                )
            else:
                for name, value in defaults.items():
                    setattr(digest, name, value)
                digest.save(update_fields=[*defaults, "updated_at"])
                digest.items.all().delete()
            positions = defaultdict(int)
            for bucket, event in ranked_items:
                positions[bucket] += 1
                IntelligenceDigestItem.objects.create(
                    digest=digest,
                    **_item_values(event, bucket, positions[bucket]),
                )
            changed = True

    run.status = CollectionRun.STATUS_SUCCESS
    run.discovered_count = len(ranked_items)
    run.created_count = len(ranked_items) if changed else 0
    run.ignored_count = 0 if changed else len(ranked_items)
    run.selected_count = sum(
        bucket != IntelligenceDigestItem.BUCKET_REVIEW for bucket, _event in ranked_items
    )
    run.review_count = sum(
        bucket == IntelligenceDigestItem.BUCKET_REVIEW for bucket, _event in ranked_items
    )
    run.finished_at = timezone.now()
    run.parameters["input_fingerprint"] = fingerprint
    run.save()
    return digest, changed, run


def analyze_digest_candidates(*, family, member, user, event_ids, provider_id=None):
    unique_ids = list(dict.fromkeys(int(value) for value in event_ids))
    if not unique_ids:
        raise IntelligenceDigestError("请选择需要 AI 整理的候选。")
    if len(unique_ids) > MAX_BATCH_ANALYSES:
        raise IntelligenceDigestError(f"一次最多整理 {MAX_BATCH_ANALYSES} 条候选。")
    provider = resolve_text_ai_provider(provider_id)
    policy = intelligence_provider_policy(provider)
    events = list(
        IntelligenceEvent.objects.filter(
            family=family,
            pk__in=unique_ids,
            channel=IntelligenceEvent.CHANNEL_PEOPLE,
            merged_into__isnull=True,
            review_status=IntelligenceEvent.REVIEW_PENDING,
            selection_status=IntelligenceEvent.SELECTION_REVIEW,
        ).order_by("pk")
    )
    if not events:
        raise IntelligenceDigestError("所选项目中没有仍处于待复核状态的本家庭候选。")

    maximum_cost = (
        policy["max_estimated_usd"] * Decimal(len(events))
    ).quantize(Decimal("0.000001"))
    run = CollectionRun.objects.create(
        family=family,
        run_kind=CollectionRun.KIND_PROCESSING,
        status=CollectionRun.STATUS_RUNNING,
        parameters={
            "operation": "digest_candidate_analysis",
            "event_ids": [event.pk for event in events],
            "provider_id": provider.pk,
            "provider_name": provider.name,
            "model_name": provider.model_name,
            "data_scope": policy["data_scope"],
            "maximum_cost_estimate_usd": str(maximum_cost),
            "max_batch_analyses": MAX_BATCH_ANALYSES,
        },
        discovered_count=len(events),
        created_by=user,
    )
    failures = []
    for event in events:
        try:
            _analysis, created = analyze_event(
                event,
                member=member,
                user=user,
                provider_id=provider.pk,
            )
        except IntelligenceAiError as exc:
            failures.append(f"事件 #{event.pk}：{exc}")
            run.failed_count += 1
        else:
            run.classified_count += 1
            if created:
                run.updated_count += 1
            else:
                run.ignored_count += 1
    if failures and run.classified_count:
        run.status = CollectionRun.STATUS_PARTIAL
    elif failures:
        run.status = CollectionRun.STATUS_FAILED
    else:
        run.status = CollectionRun.STATUS_SUCCESS
    run.error_summary = "\n".join(failures)[:4000]
    run.finished_at = timezone.now()
    run.save()
    return run
