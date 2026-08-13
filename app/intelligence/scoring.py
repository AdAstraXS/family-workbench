from dataclasses import dataclass

from .models import IntelligenceEvent, IntelligenceSource


POLICY_VERSION = "people-v1"

IMPORTANCE_WEIGHTS = {
    "relevance": 0.25,
    "impact": 0.25,
    "novelty": 0.20,
    "actionability": 0.15,
    "timeliness": 0.15,
}

CONFIDENCE_WEIGHTS = {
    "source_quality": 0.40,
    "evidence_completeness": 0.30,
    "corroboration": 0.20,
    "extraction_confidence": 0.10,
}

SOURCE_QUALITY_SCORES = {
    IntelligenceSource.TIER_A: 95,
    IntelligenceSource.TIER_B: 80,
    IntelligenceSource.TIER_C: 65,
    IntelligenceSource.TIER_D: 40,
}

THRESHOLDS = {
    "selected_importance": 75,
    "selected_confidence": 60,
    "noise_relevance": 30,
    "noise_importance": 25,
}


def _bounded(value):
    return max(0, min(100, int(value)))


def _weighted_score(values, weights):
    return round(sum(_bounded(values[name]) * weight for name, weight in weights.items()))


def evidence_completeness_score(*, has_url, has_excerpt, has_title=True):
    if has_url and has_excerpt:
        return 100
    if has_url:
        return 80
    if has_title:
        return 50
    return 20


def corroboration_score(source_count):
    return min(100, 30 + max(0, int(source_count) - 1) * 35)


@dataclass(frozen=True)
class ScoringResult:
    importance_score: int
    confidence_score: int
    selection_status: str
    breakdown: dict


def calculate_event_scores(
    *,
    relevance,
    impact,
    novelty,
    actionability,
    timeliness,
    source_tier,
    has_url,
    has_excerpt,
    source_count=1,
    extraction_confidence=100,
    change_type=IntelligenceEvent.CHANGE_UNKNOWN,
    review_status=IntelligenceEvent.REVIEW_PUBLISHED,
):
    importance_inputs = {
        "relevance": _bounded(relevance),
        "impact": _bounded(impact),
        "novelty": _bounded(novelty),
        "actionability": _bounded(actionability),
        "timeliness": _bounded(timeliness),
    }
    confidence_inputs = {
        "source_quality": SOURCE_QUALITY_SCORES[source_tier],
        "evidence_completeness": evidence_completeness_score(
            has_url=has_url,
            has_excerpt=has_excerpt,
        ),
        "corroboration": corroboration_score(source_count),
        "extraction_confidence": _bounded(extraction_confidence),
    }
    importance_score = _weighted_score(importance_inputs, IMPORTANCE_WEIGHTS)
    confidence_score = _weighted_score(confidence_inputs, CONFIDENCE_WEIGHTS)

    if (
        importance_inputs["relevance"] < THRESHOLDS["noise_relevance"]
        or importance_score < THRESHOLDS["noise_importance"]
        or review_status == IntelligenceEvent.REVIEW_IGNORED
    ):
        selection_status = IntelligenceEvent.SELECTION_NOISE
    elif (
        review_status == IntelligenceEvent.REVIEW_PENDING
        or (
            change_type == IntelligenceEvent.CHANGE_REVERSED
            and review_status != IntelligenceEvent.REVIEW_REVIEWED
        )
        or (
            importance_score >= THRESHOLDS["selected_importance"]
            and confidence_score < THRESHOLDS["selected_confidence"]
        )
    ):
        selection_status = IntelligenceEvent.SELECTION_REVIEW
    elif (
        importance_score >= THRESHOLDS["selected_importance"]
        and confidence_score >= THRESHOLDS["selected_confidence"]
    ):
        selection_status = IntelligenceEvent.SELECTION_SELECTED
    else:
        selection_status = IntelligenceEvent.SELECTION_FEED

    return ScoringResult(
        importance_score=importance_score,
        confidence_score=confidence_score,
        selection_status=selection_status,
        breakdown={
            "policy_version": POLICY_VERSION,
            "importance": {
                "inputs": importance_inputs,
                "weights": IMPORTANCE_WEIGHTS,
                "result": importance_score,
            },
            "confidence": {
                "inputs": confidence_inputs,
                "weights": CONFIDENCE_WEIGHTS,
                "result": confidence_score,
            },
            "thresholds": THRESHOLDS,
            "selection_status": selection_status,
        },
    )


def rescore_event(event, *, source_tier=None, extraction_confidence=100):
    evidence = event.evidence_links.select_related("source_item__source").order_by("-is_primary", "pk").first()
    if evidence:
        source_tier = source_tier or evidence.source_item.source.source_tier
        has_url = bool(evidence.source_item.canonical_url)
        has_excerpt = bool(evidence.excerpt or evidence.source_item.excerpt)
    else:
        source_tier = source_tier or IntelligenceSource.TIER_D
        has_url = False
        has_excerpt = False
    source_count = event.evidence_links.values("source_item__source_id").distinct().count()
    result = calculate_event_scores(
        relevance=event.relevance_score,
        impact=event.impact_score,
        novelty=event.novelty_score,
        actionability=event.actionability_score,
        timeliness=event.timeliness_score,
        source_tier=source_tier,
        has_url=has_url,
        has_excerpt=has_excerpt,
        source_count=source_count,
        extraction_confidence=extraction_confidence,
        change_type=event.change_type,
        review_status=event.review_status,
    )
    event.importance_score = result.importance_score
    event.confidence_score = result.confidence_score
    event.selection_status = result.selection_status
    event.scoring_policy_version = POLICY_VERSION
    event.scoring_breakdown = result.breakdown
    event.save(
        update_fields=[
            "importance_score",
            "confidence_score",
            "selection_status",
            "scoring_policy_version",
            "scoring_breakdown",
            "updated_at",
        ]
    )
    return result
