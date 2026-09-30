"""Bounded personal analysis; no calls from GET, no implicit cloud permission."""

import json
import os
from pathlib import Path
from urllib.request import Request
from django.conf import settings
from django.db import transaction
from knowledge.ai import _chat_url, KnowledgeAiError
from investment_research.research_ai import (
    research_provider_policy,
    ResearchAiError,
    _cost,
    _default_transport,
)
from monitoring.metering import tracked_call
from .models import WatchConsent, ResearchCandidate, ThesisEvidence
from .services import writer, dossier_for, digest, WatchError, candidate_stale
from .budget import amount, reserve, settle

PROMPT_VERSION = "watch-evidence-v2"
_UNSET = object()


def analysis_key(candidate, provider, relation=_UNSET):
    from .events import latest_relation

    if relation is _UNSET:
        relation = latest_relation(candidate.material_version)
    return digest(
        [
            candidate.dossier.owner_id,
            candidate.dossier.family_id,
            candidate.revision_id,
            candidate.material_version_id,
            candidate.rule_version,
            provider_signature(provider),
            PROMPT_VERSION,
            relation.pk if relation else None,
        ]
    )


def provider_signature(provider):
    return digest(
        [
            provider.pk,
            provider.base_url,
            provider.model_name,
            provider.execution_location,
            provider.extra_data,
        ]
    )


def targets(revision):
    if not revision:
        return {}
    return {
        **{f"pillar:{i}": text for i, text in enumerate(revision.pillars)},
        **{f"question:{i}": text for i, text in enumerate(revision.questions)},
    }


@transaction.atomic
def set_consent(member, dossier_id, provider, active):
    writer(member)
    dossier = dossier_for(member, dossier_id, lock=True)
    if active:
        research_provider_policy(provider)
        if amount(provider.extra_data.get("watch_usd_cny", 0)) <= 0:
            raise WatchError("请先配置模型美元/人民币费用换算值。")
    return WatchConsent.objects.update_or_create(
        dossier=dossier,
        defaults={
            "provider": provider,
            "active": active,
            "authorized_by": member,
            "provider_signature": provider_signature(provider),
        },
    )[0]


def validate_result(raw, candidate):
    value = json.loads(raw)
    if not isinstance(value, dict) or not isinstance(value.get("assessments"), list):
        raise WatchError("模型输出结构不正确。")
    allowed = targets(candidate.dossier.current_revision)
    text = candidate.material_version.title + "\n" + candidate.material_version.summary
    rows = []
    seen = set()
    for item in value["assessments"]:
        if not isinstance(item, dict):
            raise WatchError("逐项分析格式错误。")
        key = item.get("assumption_key")
        direction = item.get("direction")
        reason = item.get("explanation")
        quote = item.get("quote", "")
        if (
            key not in allowed
            or key in seen
            or direction not in {"support", "weaken", "mixed", "unknown"}
        ):
            raise WatchError("模型引用了不存在的假设或重复条目。")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 1200:
            raise WatchError("模型没有提供有效解释。")
        if not isinstance(quote, str) or len(quote) > 800:
            raise WatchError("模型引文无效。")
        # Exact substring validation; missing or invented quotes cannot become support/weakening.
        if quote and quote not in text:
            raise WatchError("模型引文不在本次材料中。")
        if direction != "unknown" and (len(quote.strip()) < 8 or quote not in text):
            raise WatchError("方向性结论必须提供可核查引文。")
        for field in ("conditions", "gaps", "source_claim", "author_opinion"):
            if (
                not isinstance(item.get(field, ""), str)
                or len(item.get(field, "")) > 1200
            ):
                raise WatchError("条件或缺口格式无效。")
        start = text.find(quote) if quote else 0
        rows.append(
            {
                "assumption_key": key,
                "direction": direction,
                "explanation": reason,
                "quote": quote,
                "locator": f"text:{start}:{start + len(quote)}" if quote else "",
                "conditions": item.get("conditions", ""),
                "gaps": item.get("gaps", ""),
                "source_claim": item.get("source_claim", ""),
                "author_opinion": item.get("author_opinion", ""),
            }
        )
        seen.add(key)
    if not rows or seen != set(allowed):
        raise WatchError("模型没有覆盖全部假设，保留为待分析。")
    return rows


def analyze_candidate(candidate_id, transport=None, url_validator=None):
    candidate = ResearchCandidate.objects.select_related(
        "dossier__owner__family",
        "dossier__current_revision",
        "material_version__material",
    ).get(pk=candidate_id)
    member = candidate.dossier.owner
    writer(member)
    if not getattr(settings, "INVESTMENT_WATCH_MODEL_ENABLED", False):
        raise WatchError("模型调用未启用，材料保留为待分析候选。")
    if candidate_stale(candidate) or not targets(candidate.dossier.current_revision):
        raise WatchError("材料或判断已变更，或尚未保存正式假设。")
    from .events import canonical_candidate

    canonical = canonical_candidate(candidate)
    if canonical.pk != candidate.pk:
        return analyze_candidate(
            canonical.pk, transport=transport, url_validator=url_validator
        )
    consent = (
        WatchConsent.objects.select_related("provider")
        .filter(dossier=candidate.dossier, active=True)
        .first()
    )
    if (
        not consent
        or consent.authorized_by_id != member.pk
        or consent.provider_signature != provider_signature(consent.provider)
    ):
        raise WatchError("缺少针对当前模型和个人假设的有效授权。")
    provider = consent.provider
    try:
        policy = research_provider_policy(provider)
        endpoint = (url_validator or _chat_url)(provider)
    except (ResearchAiError, KnowledgeAiError, ValueError) as exc:
        raise WatchError(str(exc)) from exc
    exchange = amount(provider.extra_data.get("watch_usd_cny", 0))
    if exchange <= 0:
        raise WatchError("缺少模型费用换算值。")
    api_key = os.getenv(policy["api_key_env_var"], "")
    if not api_key:
        raise WatchError("模型密钥尚未配置。")
    revision = candidate.dossier.current_revision
    from .events import latest_relation

    relation = latest_relation(candidate.material_version)
    key = analysis_key(candidate, provider, relation)
    if ThesisEvidence.objects.filter(input_key=key).exists():
        return 0
    prompt = (Path(__file__).parent / "prompts" / "evidence.txt").read_text(
        encoding="utf-8"
    )
    previous = None
    if relation and relation.target and relation.kind in {"followup", "conflict"}:
        previous = {
            "relation_id": relation.pk,
            "kind": relation.kind,
            "title": relation.target.title,
            "excerpt": relation.target.summary,
            "review_reason": relation.reason,
        }
    user = json.dumps(
        {
            "thesis": revision.thesis[:3000],
            "targets": targets(revision),
            "title": candidate.material_version.title,
            "excerpt": candidate.material_version.summary,
            "published_at": str(candidate.material_version.published_at),
            "previous_material": previous,
            "status": candidate.material_version.status,
        },
        ensure_ascii=False,
    )
    if len(prompt) + len(user) > policy["max_input_chars"]:
        raise WatchError("输入超出模型上限。")
    output_limit = min(policy["max_output_tokens"], 2000)
    payload = {
        "model": provider.model_name,
        "temperature": 0,
        "max_tokens": output_limit,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": user},
        ],
    }
    body = json.dumps(payload, ensure_ascii=False).encode()
    worst_usd = _cost(len(body), output_limit, policy)
    if worst_usd > policy["max_cost"]:
        raise WatchError("本次分析超过模型单次费用限制。")
    receipt = reserve(member, provider, key, worst_usd * exchange)
    try:
        request = Request(
            endpoint,
            data=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        response = tracked_call(
            lambda: (transport or _default_transport)(request, timeout=60),
            provider=provider,
            module="investment_watch",
            family_id=member.family_id,
            source=receipt.pk,
        )
        result = json.loads(response)
        choice = result["choices"][0]
        if choice.get("finish_reason") not in (None, "stop"):
            raise WatchError("模型未完整返回结果。")
        rows = validate_result(choice["message"]["content"], candidate)
        with transaction.atomic():
            locked = dossier_for(member, candidate.dossier_id, lock=True)
            # Freeze against the input revision; changed inputs are retained as stale history.
            for row in rows:
                ThesisEvidence.objects.create(
                    candidate=candidate,
                    revision=revision,
                    input_key=key,
                    input_relation=relation,
                    **row,
                )
            candidate.status = (
                "stale" if locked.current_revision_id != revision.pk else "analyzed"
            )
            candidate.save(update_fields=["status", "updated_at"])
        usage = result.get("usage", {})
        counts = [usage.get("prompt_tokens"), usage.get("completion_tokens")]
        actual = (
            _cost(*counts, policy) * exchange
            if all(type(x) is int and x >= 0 for x in counts)
            else None
        )
        settle(receipt, actual)
        return len(rows)
    except Exception:
        settle(receipt, failed=True)
        candidate.status = "failed"
        candidate.save(update_fields=["status", "updated_at"])
        raise WatchError(
            "分析未完成或引用校验失败；保留候选及费用预留，请查看运行记录。"
        ) from None
