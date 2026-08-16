import hashlib
import re
from dataclasses import dataclass
from datetime import timedelta

from django.utils import timezone

from family_core.models import Family

from .models import (
    EventEvidence,
    EventSubject,
    IntelligenceEvent,
    SourceItem,
    SubjectFollow,
)
from .scoring import POLICY_VERSION, SOURCE_QUALITY_SCORES, calculate_event_scores


RELEVANCE_THRESHOLD = 30
CLUSTER_SIMILARITY_THRESHOLD = 0.68
MEDIA_DISCOVERY_POLICY = "named-subject-v1"

KEYWORD_GROUPS = {
    "ai": (
        "artificial intelligence", " ai ", "agent", "model", "inference", "training",
        "gpu", "chip", "compute", "data center", "robot", "人工智能", "智能体",
        "大模型", "推理", "训练", "芯片", "算力", "数据中心", "机器人",
    ),
    "investment": (
        "invest", "portfolio", "holding", "capital allocation", "market", "stock",
        "earnings", "revenue", "margin", "guidance", "投资", "持仓", "资本配置",
        "market update", "in the know", "big ideas", "市场", "股票", "财报", "营收",
        "利润率", "指引",
    ),
    "policy": (
        "policy", "tariff", "regulation", "government", "white house", "sanction",
        "政策", "关税", "监管", "政府", "白宫", "制裁",
    ),
    "interview": (
        "interview", "keynote", "fireside", "podcast", "remarks", "speech", "q&a",
        "访谈", "主题演讲", "播客", "讲话", "问答",
    ),
    "business": (
        "launch", "release", "announce", "partnership", "acquire", "product",
        "推出", "发布", "宣布", "合作", "收购", "产品",
    ),
}

STOP_WORDS = {
    "the", "and", "for", "with", "from", "into", "official", "video", "new",
    "a", "an", "of", "to", "in", "on", "at", "by", "is", "are",
}

EXTRACTION_CONFIDENCE = {
    SourceItem.DEPTH_TITLE: 20,
    SourceItem.DEPTH_DESCRIPTION: 45,
    SourceItem.DEPTH_OFFICIAL_ARTICLE: 75,
    SourceItem.DEPTH_TRANSCRIPT: 90,
    SourceItem.DEPTH_MANUAL: 100,
}


@dataclass(frozen=True)
class ProcessingResult:
    is_noise: bool
    event_count: int
    relevance_score: int
    labels: list[str]


def _normalized_text(value):
    return re.sub(r"\s+", " ", (value or "").strip().casefold())


def _contains(text, keyword):
    keyword = keyword.casefold()
    if keyword.strip() != keyword:
        return keyword in f" {text} "
    return keyword in text


def _subject_terms(subject):
    values = [subject.canonical_name, subject.display_name, *(subject.aliases or [])]
    return sorted({_normalized_text(value) for value in values if len(_normalized_text(value)) >= 2}, key=len, reverse=True)


def classify_source_item(item):
    title = _normalized_text(item.title)
    excerpt = _normalized_text(item.excerpt)
    source = item.source
    topics = list(source.topics.filter(is_active=True))
    matched = []
    title_matched = []
    explicit_title_matches = 0
    explicit_excerpt_matches = 0
    for topic in topics:
        terms = _subject_terms(topic)
        title_hit = any(term in title for term in terms)
        excerpt_hit = any(term in excerpt for term in terms)
        if title_hit:
            title_matched.append(topic)
        if title_hit or excerpt_hit or topic.pk == source.subject_id:
            matched.append(topic)
        explicit_title_matches += int(title_hit)
        explicit_excerpt_matches += int(excerpt_hit)

    labels = []
    for label, keywords in KEYWORD_GROUPS.items():
        if any(_contains(title, keyword) or _contains(excerpt, keyword) for keyword in keywords):
            labels.append(label)

    relevance = 10
    if source.subject_id:
        relevance += 10
    relevance += min(45, explicit_title_matches * 30)
    relevance += min(25, explicit_excerpt_matches * 15)
    if any(any(_contains(title, keyword) for keyword in keywords) for keywords in KEYWORD_GROUPS.values()):
        relevance += 25
    elif labels:
        relevance += 15
    if item.content_depth == SourceItem.DEPTH_TITLE:
        relevance -= 5
    relevance = max(0, min(100, relevance))
    gate_reason = ""
    if source.extra_data.get("discovery_policy") == MEDIA_DISCOVERY_POLICY:
        matched = title_matched
        if not matched:
            relevance = min(relevance, RELEVANCE_THRESHOLD - 1)
            gate_reason = "媒体发现源标题未直接出现关注对象"
    return matched, relevance, labels, gate_reason


def _cluster_tokens(title):
    normalized = _normalized_text(title)
    latin_tokens = {
        token for token in re.findall(r"[a-z0-9]+", normalized)
        if len(token) > 1 and token not in STOP_WORDS
    }
    cjk_blocks = re.findall(r"[\u3400-\u9fff]+", normalized)
    cjk_tokens = set()
    for block in cjk_blocks:
        if len(block) == 1:
            cjk_tokens.add(block)
        else:
            cjk_tokens.update(block[index:index + 2] for index in range(len(block) - 1))
    return latin_tokens | cjk_tokens


def _similarity(left, right):
    left_tokens = _cluster_tokens(left)
    right_tokens = _cluster_tokens(right)
    if not left_tokens or not right_tokens:
        return 0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def _cluster_key(title, occurred_at):
    token_string = "|".join(sorted(_cluster_tokens(title))) or _normalized_text(title)
    day = occurred_at.date().isoformat()
    return hashlib.sha256(f"{day}|{token_string}".encode("utf-8")).hexdigest()


def _event_type(labels):
    if "investment" in labels:
        return IntelligenceEvent.TYPE_INVESTMENT
    if "policy" in labels:
        return IntelligenceEvent.TYPE_POLICY
    if "interview" in labels:
        return IntelligenceEvent.TYPE_INTERVIEW
    if "business" in labels or "ai" in labels:
        return IntelligenceEvent.TYPE_BUSINESS
    return IntelligenceEvent.TYPE_OTHER


def _timeliness_score(published_at):
    age = timezone.now() - published_at
    if age <= timedelta(days=1):
        return 90
    if age <= timedelta(days=7):
        return 70
    if age <= timedelta(days=30):
        return 50
    return 30


def _candidate_features(relevance, labels, published_at):
    impact = 65 if {"investment", "policy", "business"} & set(labels) else 45
    novelty = 65
    actionability = 65 if "investment" in labels else 45
    return {
        "relevance": relevance,
        "impact": impact,
        "novelty": novelty,
        "actionability": actionability,
        "timeliness": _timeliness_score(published_at),
    }


def _find_existing_event(family, item, matched_topics, occurred_at):
    exact_key = _cluster_key(item.title, occurred_at)
    exact = IntelligenceEvent.objects.filter(family=family, cluster_key=exact_key).first()
    if exact:
        return exact, exact_key
    candidates = (
        IntelligenceEvent.objects.filter(
            family=family,
            occurred_at__gte=occurred_at - timedelta(days=2),
            occurred_at__lte=occurred_at + timedelta(days=2),
            subjects__in=matched_topics,
        )
        .distinct()
        .order_by("-occurred_at")[:30]
    )
    for candidate in candidates:
        if _similarity(candidate.title, item.title) >= CLUSTER_SIMILARITY_THRESHOLD:
            return candidate, candidate.cluster_key or exact_key
    return None, exact_key


def _score_event(event, *, source_tier, extraction_confidence, source_count):
    result = calculate_event_scores(
        relevance=event.relevance_score,
        impact=event.impact_score,
        novelty=event.novelty_score,
        actionability=event.actionability_score,
        timeliness=event.timeliness_score,
        source_tier=source_tier,
        has_url=bool(event.primary_source_item and event.primary_source_item.canonical_url),
        has_excerpt=bool(event.primary_source_item and event.primary_source_item.excerpt),
        source_count=source_count,
        extraction_confidence=extraction_confidence,
        change_type=event.change_type,
        review_status=event.review_status,
    )
    breakdown = dict(result.breakdown)
    breakdown["automated_candidate"] = True
    breakdown["content_depth"] = event.primary_source_item.content_depth if event.primary_source_item else SourceItem.DEPTH_TITLE
    event.importance_score = result.importance_score
    event.confidence_score = result.confidence_score
    event.selection_status = result.selection_status
    event.scoring_policy_version = POLICY_VERSION
    event.scoring_breakdown = breakdown
    event.save(update_fields=[
        "importance_score", "confidence_score", "selection_status",
        "scoring_policy_version", "scoring_breakdown", "updated_at",
    ])


def _upsert_candidate_event(family, item, matched_topics, relevance, labels):
    occurred_at = item.published_at or item.fetched_at
    event, cluster_key = _find_existing_event(family, item, matched_topics, occurred_at)
    features = _candidate_features(relevance, labels, occurred_at)
    created = event is None
    if created:
        excerpt = item.excerpt[:600].strip()
        if item.source.source_group == item.source.GROUP_MEDIA:
            source_label = "媒体信源"
        elif item.source.source_group == item.source.GROUP_OFFICIAL:
            source_label = "官方信源"
        else:
            source_label = "外部信源"
        summary = f"自动采集到{source_label}条目《{item.title}》。"
        if excerpt:
            summary += f"来源简介：{excerpt}"
        summary += " 当前尚未核查完整正文或视频内容。"
        event = IntelligenceEvent.objects.create(
            family=family,
            channel=IntelligenceEvent.CHANNEL_PEOPLE,
            event_type=_event_type(labels),
            title=item.title,
            occurred_at=occurred_at,
            occurred_precision=IntelligenceEvent.PRECISION_EXACT,
            summary=summary,
            why_it_matters="自动采集候选，需管理员结合原文确认重要性和具体陈述。",
            relevance_score=features["relevance"],
            impact_score=features["impact"],
            novelty_score=features["novelty"],
            actionability_score=features["actionability"],
            timeliness_score=features["timeliness"],
            change_type=IntelligenceEvent.CHANGE_UNKNOWN,
            review_status=IntelligenceEvent.REVIEW_PENDING,
            selection_status=IntelligenceEvent.SELECTION_REVIEW,
            scoring_policy_version=POLICY_VERSION,
            score_origin=IntelligenceEvent.SCORE_ORIGIN_RULES,
            cluster_key=cluster_key,
            primary_source_item=item,
        )
    else:
        event.last_seen_at = timezone.now()
        event.save(update_fields=["last_seen_at", "updated_at"])

    for index, topic in enumerate(matched_topics):
        EventSubject.objects.get_or_create(
            event=event,
            subject=topic,
            role=EventSubject.ROLE_SUBJECT,
            defaults={"confidence_score": 80, "is_primary": index == 0},
        )
    _evidence, evidence_created = EventEvidence.objects.get_or_create(
        event=event,
        source_item=item,
        defaults={
            "evidence_type": EventEvidence.TYPE_CONTEXT,
            "excerpt": item.excerpt,
            "source_quality_score": SOURCE_QUALITY_SCORES[item.source.source_tier],
            "is_primary": created,
        },
    )
    if evidence_created and not created:
        event.analyses.filter(is_current=True).update(is_current=False)
    source_count = event.evidence_links.values("source_item__source_id").distinct().count()
    tiers = list(event.evidence_links.values_list("source_item__source__source_tier", flat=True))
    best_tier = min(tiers or [item.source.source_tier])
    depths = list(event.evidence_links.values_list("source_item__content_depth", flat=True))
    extraction_confidence = max(EXTRACTION_CONFIDENCE.get(depth, 20) for depth in depths or [item.content_depth])
    _score_event(
        event,
        source_tier=best_tier,
        extraction_confidence=extraction_confidence,
        source_count=source_count,
    )
    return event, created


def process_source_item(item):
    matched_topics, relevance, labels, gate_reason = classify_source_item(item)
    item.matched_subjects.set(matched_topics)
    item.classification_labels = labels
    item.relevance_score = relevance
    item.processed_at = timezone.now()
    if relevance < RELEVANCE_THRESHOLD:
        item.processing_status = SourceItem.STATUS_NOISE
        reason_prefix = f"{gate_reason}；" if gate_reason else ""
        item.processing_reason = (
            f"{reason_prefix}规则相关性 {relevance}，低于 {RELEVANCE_THRESHOLD}；"
            "保留原始条目但不生成事件。"
        )
        item.save(update_fields=[
            "classification_labels", "relevance_score", "processed_at",
            "processing_status", "processing_reason", "updated_at",
        ])
        return ProcessingResult(True, 0, relevance, labels)

    followed_family_ids = SubjectFollow.objects.filter(
        subject__in=matched_topics,
        is_active=True,
    ).values_list("family_id", flat=True)
    families = list(Family.objects.filter(pk__in=followed_family_ids).distinct())
    event_count = 0
    created_count = 0
    for family in families:
        _event, created = _upsert_candidate_event(family, item, matched_topics, relevance, labels)
        event_count += 1
        created_count += int(created)

    item.processing_status = SourceItem.STATUS_CLUSTERED if families else SourceItem.STATUS_SCORED
    if created_count:
        item.processing_reason = f"规则相关性 {relevance}；已生成 {created_count} 个家庭待复核候选。"
    elif event_count:
        item.processing_reason = f"规则相关性 {relevance}；已并入既有候选事件。"
    else:
        item.processing_reason = f"规则相关性 {relevance}；相关主题暂未被任何家庭关注。"
    item.save(update_fields=[
        "classification_labels", "relevance_score", "processed_at",
        "processing_status", "processing_reason", "updated_at",
    ])
    return ProcessingResult(False, event_count, relevance, labels)
