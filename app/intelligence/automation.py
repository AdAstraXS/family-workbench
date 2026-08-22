from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from .ai_enrichment import (
    IntelligenceAiError,
    analyze_event,
    intelligence_provider_policy,
    resolve_text_ai_provider,
)
from .article_evidence import fetch_article_evidence
from .collection import collect_intelligence_sources
from .digest import IntelligenceDigestError, generate_daily_digest
from .models import CollectionRun, EventAnalysis, IntelligenceEvent, IntelligenceSource, SourceItem
from .scoring import rescore_event
from .processing import process_source_item


MAX_AUTO_ANALYSES_PER_RUN = 3
MAX_AUTO_ANALYSES_PER_DAY = 5
MAX_AUTO_DAILY_ESTIMATED_USD = Decimal("0.050000")
AUTOMATION_LOOKBACK_HOURS = 36
RUN_LEASE_MINUTES = 45
MAX_ARTICLE_FETCHES_PER_RUN = 3


@dataclass(frozen=True)
class AutomationCycleResult:
    run: CollectionRun
    collection_run: CollectionRun | None
    digest_id: int | None
    skipped: bool = False


def _day_start():
    local_date = timezone.localdate()
    return timezone.make_aware(
        datetime.combine(local_date, time.min),
        timezone.get_current_timezone(),
    )


def _candidate_events(family, *, limit):
    current_success = EventAnalysis.objects.filter(
        event_id=OuterRef("pk"),
        status=EventAnalysis.STATUS_SUCCESS,
        is_current=True,
    )
    return list(
        IntelligenceEvent.objects.filter(
            family=family,
            channel=IntelligenceEvent.CHANNEL_PEOPLE,
            merged_into__isnull=True,
            review_status=IntelligenceEvent.REVIEW_PENDING,
            selection_status=IntelligenceEvent.SELECTION_REVIEW,
            occurred_at__gte=timezone.now() - timedelta(hours=AUTOMATION_LOOKBACK_HOURS),
            primary_source_item__source__source_tier__in=[
                IntelligenceSource.TIER_A,
                IntelligenceSource.TIER_B,
                IntelligenceSource.TIER_C,
            ],
        )
        .annotate(has_current_analysis=Exists(current_success))
        .filter(has_current_analysis=False, relevance_score__gte=45, importance_score__gte=40)
        .select_related("primary_source_item__source")
        .order_by("-importance_score", "-confidence_score", "-occurred_at", "-pk")[:limit]
    )


def _fill_missing_article_evidence(family):
    items = list(
        SourceItem.objects.filter(
            source__adapter_key=IntelligenceSource.ADAPTER_RSS,
            source__extra_data__article_fetch_policy=IntelligenceSource.ARTICLE_FETCH_PUBLIC_HTML,
            canonical_url__gt="",
            event_evidence_links__event__family=family,
            event_evidence_links__event__merged_into__isnull=True,
            event_evidence_links__event__review_status=IntelligenceEvent.REVIEW_PENDING,
            event_evidence_links__event__occurred_at__gte=(
                timezone.now() - timedelta(hours=AUTOMATION_LOOKBACK_HOURS)
            ),
        )
        .filter(
            Q(article_fetch_status=SourceItem.ARTICLE_NOT_REQUESTED)
            | Q(
                article_fetch_status=SourceItem.ARTICLE_FAILED,
                article_fetched_at__lte=timezone.now() - timedelta(hours=6),
            )
        )
        .select_related("source")
        .prefetch_related("source__topics")
        .distinct()
        .order_by("-published_at", "-fetched_at", "-pk")[:MAX_ARTICLE_FETCHES_PER_RUN]
    )
    extracted = 0
    failed = 0
    for item in items:
        result = fetch_article_evidence(item)
        if result.status == SourceItem.ARTICLE_EXTRACTED:
            process_source_item(item)
            extracted += 1
        elif result.status == SourceItem.ARTICLE_FAILED:
            failed += 1
    return len(items), extracted, failed


def _source_tier(event):
    tiers = list(
        event.evidence_links.values_list("source_item__source__source_tier", flat=True)
    )
    return min(tiers or [IntelligenceSource.TIER_D])


def _record_automation_decision(event, analysis, *, decision, reason, user):
    breakdown = dict(event.scoring_breakdown or {})
    breakdown.update(
        {
            "automation_analysis_id": analysis.pk,
            "automation_decision": decision,
            "automation_reason": reason,
            "automation_decided_at": timezone.now().isoformat(),
        }
    )
    event.scoring_breakdown = breakdown
    event.updated_by = user
    event.save(update_fields=["scoring_breakdown", "updated_by", "updated_at"])


def _existing_analysis_candidates(family, *, limit=20):
    events = (
        IntelligenceEvent.objects.filter(
            family=family,
            channel=IntelligenceEvent.CHANNEL_PEOPLE,
            merged_into__isnull=True,
            review_status=IntelligenceEvent.REVIEW_PENDING,
            selection_status__in=[
                IntelligenceEvent.SELECTION_REVIEW,
                IntelligenceEvent.SELECTION_NOISE,
            ],
            occurred_at__gte=timezone.now() - timedelta(hours=AUTOMATION_LOOKBACK_HOURS),
            analyses__status=EventAnalysis.STATUS_SUCCESS,
            analyses__is_current=True,
        )
        .select_related("primary_source_item__source")
        .prefetch_related("analyses")
        .distinct()
        .order_by("-importance_score", "-confidence_score", "-occurred_at", "-pk")[:limit]
    )
    candidates = []
    for event in events:
        analysis = event.current_ai_analysis
        if analysis and (event.scoring_breakdown or {}).get("automation_analysis_id") != analysis.pk:
            candidates.append((event, analysis))
    return candidates


def _auto_route_event(event, analysis, *, user):
    result = analysis.result_json or {}
    features = result.get("features") or {}
    source_tier = _source_tier(event)
    substantiveness = int(features.get("substantiveness") or 0)
    evidence_clarity = int(features.get("evidence_clarity") or 0)

    if event.relevance_score < 30 or event.importance_score < 25:
        event.review_status = IntelligenceEvent.REVIEW_IGNORED
        event.selection_status = IntelligenceEvent.SELECTION_NOISE
        event.updated_by = user
        event.save(
            update_fields=["review_status", "selection_status", "updated_by", "updated_at"]
        )
        if event.primary_source_item_id:
            SourceItem.objects.filter(pk=event.primary_source_item_id).update(
                processing_status=SourceItem.STATUS_NOISE,
                processing_reason="AI 结构化特征低于噪音门槛；保留来源证据但不进入信息流。",
                updated_at=timezone.now(),
            )
        _record_automation_decision(
            event,
            analysis,
            decision="ignored",
            reason="AI 相关性或重要性低于噪音门槛。",
            user=user,
        )
        return "ignored"

    passes_publication_gate = (
        source_tier in {IntelligenceSource.TIER_A, IntelligenceSource.TIER_B, IntelligenceSource.TIER_C}
        and event.relevance_score >= 55
        and event.importance_score >= 50
        and event.confidence_score >= 55
        and substantiveness >= 45
        and evidence_clarity >= 45
        and event.change_type != IntelligenceEvent.CHANGE_REVERSED
        and bool(result.get("summary_evidence_refs"))
    )
    if not passes_publication_gate:
        _record_automation_decision(
            event,
            analysis,
            decision="review",
            reason="转向、证据清晰度或综合分数未同时达到自动发布门槛。",
            user=user,
        )
        return "review"

    event.review_status = IntelligenceEvent.REVIEW_AI_PUBLISHED
    event.updated_by = user
    event.save(update_fields=["review_status", "updated_by", "updated_at"])
    scoring = rescore_event(
        event,
        source_tier=source_tier,
        extraction_confidence=evidence_clarity,
    )
    if scoring.selection_status == IntelligenceEvent.SELECTION_REVIEW:
        event.review_status = IntelligenceEvent.REVIEW_PENDING
        event.updated_by = user
        event.save(update_fields=["review_status", "updated_by", "updated_at"])
        rescore_event(
            event,
            source_tier=source_tier,
            extraction_confidence=evidence_clarity,
        )
        _record_automation_decision(
            event,
            analysis,
            decision="review",
            reason="移除待复核状态后，代码评分仍要求人工复核。",
            user=user,
        )
        return "review"

    if event.primary_source_item_id:
        SourceItem.objects.filter(pk=event.primary_source_item_id).update(
            processing_status=SourceItem.STATUS_PUBLISHED,
            processing_reason="AI 已依据公开证据整理，并通过代码门槛自动进入信息流。",
            updated_at=timezone.now(),
        )
    _record_automation_decision(
        event,
        analysis,
        decision="published",
        reason="来源、相关性、实质性、证据清晰度和代码评分均通过自动门槛。",
        user=user,
    )
    return "published"


def run_intelligence_cycle(*, family, member, user=None, provider_id=None, max_items=20):
    recent_running = CollectionRun.objects.filter(
        family=family,
        run_kind=CollectionRun.KIND_AUTOMATION,
        status=CollectionRun.STATUS_RUNNING,
        started_at__gte=timezone.now() - timedelta(minutes=RUN_LEASE_MINUTES),
    ).order_by("-started_at").first()
    if recent_running:
        return AutomationCycleResult(
            run=recent_running,
            collection_run=None,
            digest_id=None,
            skipped=True,
        )

    run = CollectionRun.objects.create(
        family=family,
        run_kind=CollectionRun.KIND_AUTOMATION,
        status=CollectionRun.STATUS_RUNNING,
        parameters={
            "operation": "m4_1_automatic_cycle",
            "max_items_per_source": max_items,
            "max_ai_per_run": MAX_AUTO_ANALYSES_PER_RUN,
            "max_ai_per_day": MAX_AUTO_ANALYSES_PER_DAY,
            "max_daily_estimated_usd": str(MAX_AUTO_DAILY_ESTIMATED_USD),
            "public_article_policy": "source_opt_in_minimum_evidence_only",
        },
        created_by=user,
    )
    errors = []
    collection_run = collect_intelligence_sources(
        due_only=True,
        max_items=max_items,
        created_by=user,
        family=family,
    )
    run.parameters["collection_run_id"] = collection_run.pk
    if collection_run.status != CollectionRun.STATUS_SUCCESS:
        errors.append(f"采集运行 #{collection_run.pk} 未完全成功。")

    article_attempted, article_extracted, article_failed = _fill_missing_article_evidence(family)
    run.parameters["article_evidence_attempted"] = article_attempted
    run.parameters["article_evidence_extracted"] = article_extracted
    run.parameters["article_evidence_failed"] = article_failed
    if article_failed:
        errors.append(f"{article_failed} 个公开网页证据提取失败，相关条目已降级使用订阅元数据。")

    routed_existing = _existing_analysis_candidates(family)
    run.parameters["existing_analysis_event_ids"] = [
        event.pk for event, _analysis in routed_existing
    ]
    for event, analysis in routed_existing:
        route = _auto_route_event(event, analysis, user=user)
        run.classified_count += 1
        if route == "published":
            run.selected_count += 1
        elif route == "ignored":
            run.noise_count += 1
        else:
            run.review_count += 1

    today_attempts = EventAnalysis.objects.filter(
        event__family=family,
        created_at__gte=_day_start(),
    ).count()
    remaining_daily = max(0, MAX_AUTO_ANALYSES_PER_DAY - today_attempts)
    analysis_limit = min(MAX_AUTO_ANALYSES_PER_RUN, remaining_daily)
    candidates = _candidate_events(family, limit=max(analysis_limit, 1)) if analysis_limit else []
    run.discovered_count = len(routed_existing) + len(candidates)
    provider = None
    policy = None
    if candidates:
        try:
            provider = resolve_text_ai_provider(provider_id)
            policy = intelligence_provider_policy(provider)
        except IntelligenceAiError as exc:
            errors.append(str(exc))
            run.failed_count += len(candidates)
            candidates = []
    if policy:
        reserved = policy["max_estimated_usd"] * Decimal(today_attempts + len(candidates))
        if reserved > MAX_AUTO_DAILY_ESTIMATED_USD:
            allowed_by_cost = max(
                0,
                int(MAX_AUTO_DAILY_ESTIMATED_USD // policy["max_estimated_usd"]) - today_attempts,
            )
            candidates = candidates[:allowed_by_cost]
        run.parameters.update(
            {
                "provider_id": provider.pk,
                "provider_name": provider.name,
                "data_scope": policy["data_scope"],
                "policy_version": policy["policy_version"],
                "ai_attempts_before_run": today_attempts,
                "selected_event_ids": [event.pk for event in candidates],
            }
        )

    for event in candidates:
        try:
            analysis, created = analyze_event(
                event,
                member=member,
                user=user,
                provider_id=provider.pk,
            )
            event.refresh_from_db()
            route = _auto_route_event(event, analysis, user=user)
        except IntelligenceAiError as exc:
            errors.append(f"事件 #{event.pk}：{exc}")
            run.failed_count += 1
            continue
        run.classified_count += 1
        run.updated_count += int(created)
        run.ignored_count += int(not created)
        if route == "published":
            run.selected_count += 1
        elif route == "ignored":
            run.noise_count += 1
        else:
            run.review_count += 1

    digest_id = None
    try:
        digest, changed, digest_run = generate_daily_digest(
            family=family,
            user=user,
        )
    except IntelligenceDigestError:
        digest = None
    else:
        digest_id = digest.pk
        run.parameters["digest_run_id"] = digest_run.pk
        run.parameters["digest_id"] = digest.pk
        run.parameters["digest_changed"] = changed

    if errors and run.classified_count:
        run.status = CollectionRun.STATUS_PARTIAL
    elif errors:
        run.status = CollectionRun.STATUS_FAILED
    else:
        run.status = CollectionRun.STATUS_SUCCESS
    run.error_summary = "\n".join(errors)[:4000]
    run.finished_at = timezone.now()
    run.save()
    return AutomationCycleResult(
        run=run,
        collection_run=collection_run,
        digest_id=digest_id,
    )
