"""One paid batch of headline/summary triage, using the existing consent and budget."""

import json
import os
from datetime import timedelta
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from knowledge.ai import _chat_url, KnowledgeAiError
from investment_research.research_ai import research_provider_policy, ResearchAiError, _cost, _default_transport
from monitoring.metering import tracked_call
from .analysis import provider_signature, targets
from .budget import amount, reserve, settle
from .catalogue import match_rule
from .models import WatchConsent, WatchRule, CandidateScreening, ScreeningBatch, WatchPipelineState, ResearchCandidate
from .services import WatchError, writer, digest, candidate_stale, dossier_for

SCREEN_VERSION = "watch-screen-v2"


def authorized_provider(dossier):
    writer(dossier.owner)
    if not getattr(settings, "INVESTMENT_WATCH_MODEL_ENABLED", False):
        raise WatchError("模型调用未启用。")
    consent = WatchConsent.objects.select_related("provider").filter(dossier=dossier, active=True).first()
    if (not consent or consent.authorized_by_id != dossier.owner_id
            or consent.provider_signature != provider_signature(consent.provider)):
        raise WatchError("缺少针对当前模型和个人假设的有效授权。")
    try:
        research_provider_policy(consent.provider)
    except ResearchAiError as exc:
        raise WatchError(str(exc)) from exc
    return consent.provider


def screening_key(candidate, provider, rule_version=None):
    if rule_version is None:
        rule = WatchRule.objects.filter(dossier=candidate.dossier).first()
        rule_version = rule.version if rule else 0
    return digest([SCREEN_VERSION, candidate.pk, candidate.revision_id,
                   candidate.material_version_id, rule_version,
                   provider_signature(provider)])


def chosen(candidate, screening):
    return bool(screening and screening.batch.status == "completed" and
                (candidate.reading_requested or screening.selected and not screening.duplicate_of_id))


def recent_context(dossier):
    """Private, bounded history used for reading deduplication, not event merging."""
    from .profiles import profile, official
    company = profile(dossier)
    rows = []
    candidates = ResearchCandidate.objects.filter(dossier=dossier, revision=dossier.current_revision,
        material_version__found_at__gte=timezone.now() - timedelta(days=3)).select_related(
        "material_version__material__source").prefetch_related("screenings__batch").order_by("-pk")
    for c in candidates:
        s = max(c.screenings.all(), key=lambda row: row.pk, default=None)
        if not candidate_stale(c) and chosen(c, s):
            v = c.material_version
            rows.append({"candidate_id": c.pk, "title": v.title, "summary": v.summary[:600],
                         "published_at": str(v.published_at) if v.published_at else None,
                         "official": official(v, company)})
            if len(rows) == 12:
                break
    return rows


def eligible(candidate, state):
    if candidate_stale(candidate):
        return False
    # History stays readable. Enabling collection never drains the historical backlog.
    if not candidate.manual and (candidate.material_version.found_at < state.started_at
            or candidate.material_version.found_at < timezone.now() - timedelta(hours=48)
            or candidate.material_version.published_at and candidate.material_version.published_at < timezone.now() - timedelta(hours=48)):
        return False
    rule = WatchRule.objects.filter(dossier=candidate.dossier, enabled=True).first()
    if candidate.manual:
        return True
    return bool(rule and match_rule(rule, candidate.material_version)[0])


def latest_screening(candidate, provider):
    return CandidateScreening.objects.filter(input_key=screening_key(candidate, provider)).first()


def validate_screening(raw, candidates, recent_ids=()):
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        raise WatchError("初筛未返回有效 JSON。") from None
    rows = value.get("decisions") if isinstance(value, dict) else None
    allowed = {c.pk for c in candidates}
    seen = set()
    if not isinstance(rows, list):
        raise WatchError("初筛结构无效。")
    for row in rows:
        if not isinstance(row, dict):
            raise WatchError("初筛条目无效。")
        pk = row.get("candidate_id")
        if type(pk) is not int or pk not in allowed or pk in seen:
            raise WatchError("初筛引用了不存在或重复的候选。")
        if type(row.get("selected")) is not bool or type(row.get("priority")) is not int or not 0 <= row["priority"] <= 100:
            raise WatchError("初筛选择或优先级无效。")
        if not isinstance(row.get("reason"), str) or not row["reason"].strip() or len(row["reason"]) > 500:
            raise WatchError("初筛必须提供简短理由。")
        relation = row.get("relevance", "unknown")
        duplicate = row.get("duplicate_of")
        if not isinstance(relation, str) or relation not in {"direct", "industry", "unknown"}:
            raise WatchError("初筛关联类型无效。")
        if duplicate is not None and (type(duplicate) is not int or duplicate == pk or duplicate not in set(recent_ids) | allowed):
            raise WatchError("初筛重复引用超出本公司可见候选。")
        if duplicate is not None and row["selected"]:
            raise WatchError("重复报道不能同时入选正文。")
        seen.add(pk)
    if seen != allowed:
        raise WatchError("初筛没有覆盖本批全部候选。")
    selected = {r["candidate_id"] for r in rows if r["selected"]}
    if any(r.get("duplicate_of") is not None and r["duplicate_of"] not in set(recent_ids) | selected for r in rows):
        raise WatchError("重复报道只能引用近期已选或本批已选材料。")
    return rows


def screen_candidates(dossier, candidates, *, transport=None, url_validator=None):
    """Reserve every input before sending; interrupted/malformed batches never auto-repeat."""
    dossier = dossier_for(dossier.owner, dossier.pk)
    provider = authorized_provider(dossier)
    if not targets(dossier.current_revision):
        raise WatchError("请先保存正式研究假设。")
    policy = research_provider_policy(provider)
    state, _ = WatchPipelineState.objects.get_or_create(family=dossier.family)
    prepared = []
    prompt = (Path(__file__).parent / "prompts" / "screening.txt").read_text(encoding="utf-8")
    from .profiles import profile, official
    company = profile(dossier)
    recent = recent_context(dossier)
    while recent and len(json.dumps(recent, ensure_ascii=False)) > policy["max_input_chars"] // 4:
        recent.pop()
    base = {"company_profile": company, "targets": targets(dossier.current_revision),
            "recent_selected": recent, "candidates": []}
    from .events import canonical_candidate
    seen = set()
    for incoming in candidates:
        if incoming.dossier_id != dossier.pk or not eligible(incoming, state):
            continue
        c = canonical_candidate(incoming)
        if not eligible(c, state):
            continue
        key = screening_key(c, provider)
        if key in seen or CandidateScreening.objects.filter(input_key=key).exists():
            continue
        seen.add(key)
        v = c.material_version
        item = {"candidate_id": c.pk, "title": v.title, "summary": v.summary,
                "source": v.material.source.name, "published_at": str(v.published_at) if v.published_at else None,
                "official": official(v, company)}
        trial = {**base, "candidates": base["candidates"] + [item]}
        if len(prompt) + len(json.dumps(trial, ensure_ascii=False)) > policy["max_input_chars"]:
            if not prepared:
                raise WatchError("初筛输入超出模型上限。")
            break
        base = trial
        prepared.append((c, key))
        if len(prepared) >= min(8, max(1, policy["max_output_tokens"] // 350)):
            break
    if not prepared:
        return 0
    try:
        endpoint = (url_validator or _chat_url)(provider)
    except (KnowledgeAiError, ValueError) as exc:
        raise WatchError(str(exc)) from exc
    api_key = os.getenv(policy["api_key_env_var"], "")
    exchange = amount(provider.extra_data.get("watch_usd_cny", 0))
    if not api_key or exchange <= 0:
        raise WatchError("初筛模型密钥或费用换算值尚未配置。")
    input_key = digest([SCREEN_VERSION, [key for _, key in prepared]])
    if ScreeningBatch.objects.filter(input_key=input_key).exists():
        return 0
    glm53 = urlsplit(provider.base_url).hostname == "open.bigmodel.cn" and provider.model_name.casefold() in {"glm-5.3", "glm-5.3-flash", "glm-5.3-flashx"}
    output_limit = min(policy["max_output_tokens"], 4000 if glm53 else 2500)
    payload = {"model": provider.model_name, "temperature": 0, "max_tokens": output_limit,
               "messages": [{"role": "system", "content": prompt}, {"role": "user", "content": json.dumps(base, ensure_ascii=False)}]}
    if glm53:
        payload.update(thinking={"type": "enabled"}, reasoning_effort="low", temperature=1, response_format={"type": "json_object"})
    body = json.dumps(payload, ensure_ascii=False).encode()
    worst = _cost(len(body), output_limit, policy)
    if worst > policy["max_cost"]:
        raise WatchError("初筛超出模型单次费用限制。")
    with transaction.atomic():
        dossier_for(dossier.owner, dossier.pk, lock=True)
        if any(CandidateScreening.objects.filter(input_key=key).exists() for _, key in prepared):
            return 0
        receipt = reserve(dossier.owner, provider, input_key, worst * exchange)
        batch = ScreeningBatch.objects.create(dossier=dossier, input_key=input_key)
        for c, key in prepared:
            CandidateScreening.objects.create(candidate=c, batch=batch, input_key=key, reason="初筛请求已预留；中断或结果未知时不自动重试。")
    actual = None
    try:
        request = Request(endpoint, data=body, headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, method="POST")
        result = json.loads(tracked_call(lambda: (transport or _default_transport)(request, timeout=60),
                                        provider=provider, module="investment_watch", family_id=dossier.family_id, source=receipt.pk))
        usage = result.get("usage", {})
        counts = [usage.get("prompt_tokens"), usage.get("completion_tokens")]
        if all(type(x) is int and x >= 0 for x in counts):
            actual = _cost(*counts, policy) * exchange
        choice = result["choices"][0]
        if choice.get("finish_reason") not in (None, "stop"):
            raise WatchError("初筛结果未完整返回。")
        rows = validate_screening(choice["message"]["content"], [c for c, _ in prepared], [r["candidate_id"] for r in recent])
        with transaction.atomic():
            locked = dossier_for(dossier.owner, dossier.pk, lock=True)
            current = locked.current_revision_id == dossier.current_revision_id
            batch.status = "completed" if current else "stale"
            for row in rows:
                CandidateScreening.objects.filter(batch=batch, candidate_id=row["candidate_id"]).update(
                    selected=row["selected"] if current else False, priority=row["priority"], reason=row["reason"],
                    relevance=row.get("relevance", "unknown"), duplicate_of_id=row.get("duplicate_of"))
            batch.save(update_fields=["status", "updated_at"])
        settle(receipt, actual)
        return len(rows)
    except Exception as exc:
        settle(receipt, actual, failed=True)
        message = str(exc) if isinstance(exc, WatchError) else (f"初筛服务返回 HTTP {exc.code}。" if isinstance(exc, HTTPError) else "初筛未完成，已保留费用预留与候选记录。")
        batch.status = "failed"
        batch.message = message[:500]
        batch.save(update_fields=["status", "message", "updated_at"])
        raise WatchError(message) from None


def ready_candidates(dossier):
    provider = authorized_provider(dossier)
    state, _ = WatchPipelineState.objects.get_or_create(family=dossier.family)
    rows = []
    from .profiles import profile, official
    company = profile(dossier)
    for c in ResearchCandidate.objects.filter(dossier=dossier, revision=dossier.current_revision).select_related(
        "dossier__owner", "dossier__security", "dossier__current_revision", "material_version__material__source"):
        screening = latest_screening(c, provider)
        if eligible(c, state) and chosen(c, screening):
            rows.append((101 if c.reading_requested else screening.priority, official(c.material_version, company), c))
    return [c for _, _, c in sorted(rows, key=lambda row: (row[0], row[1], row[2].material_version.found_at), reverse=True)]
