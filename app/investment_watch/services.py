"""Scope checks and atomic mutations shared by HTML and JSON endpoints."""

import hashlib
import json
import re
from datetime import date
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import F, Q
from django.http import Http404

from family_core.models import Family, FamilyMember
from investment_research.permissions import accessible_dossiers, is_writer
from .catalogue import TOPICS, classify, match_rule
from .models import (
    NewsSource,
    NewsMaterial,
    MaterialVersion,
    InvestmentEvent,
    WatchRule,
    ResearchCandidate,
    MemberAnnotation,
    OperationReceipt,
    ThesisEvidence,
    EvidenceReview,
    EventAction,
)


class WatchError(ValueError):
    status = 400


class Conflict(WatchError):
    status = 409


def digest(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()
    ).hexdigest()


def writer(member):
    if not is_writer(member):
        raise PermissionDenied("查看者或停用成员不能执行此操作。")


def dossier_for(member, pk, lock=False):
    query = accessible_dossiers(member)
    if lock:
        query = query.select_for_update(of=("self",))
    dossier = query.filter(pk=pk).first()
    if dossier is None:
        raise Http404("研究档案不存在。")
    return dossier


def version_for(member, pk):
    if not member or not member.is_active:
        raise PermissionDenied
    version = (
        MaterialVersion.objects.select_related("material__source", "material__event")
        .filter(pk=pk, material__source__family=member.family)
        .first()
    )
    if not version:
        raise Http404("材料不存在。")
    return version


def public_versions(member):
    if not member or not member.is_active:
        return MaterialVersion.objects.none()
    # Current records only; previously saved versions remain accessible in details.
    return MaterialVersion.objects.filter(
        material__source__family=member.family,
        material__current_version_id=F("pk"),
    ).select_related("material__source", "material__event")


def filter_news(member, params):
    query = public_versions(member).exclude(status="withdrawn")
    for key in ("market", "category"):
        if params.get(key):
            query = query.filter(**{key: params[key]})
    if params.get("source"):
        query = query.filter(material__source__key=params["source"])
    if params.get("q"):
        term = str(params["q"])[:200]
        query = query.filter(Q(title__icontains=term) | Q(summary__icontains=term))
    for key, lookup in (
        ("date_from", "published_at__date__gte"),
        ("date_to", "published_at__date__lte"),
    ):
        if params.get(key):
            try:
                value = date.fromisoformat(params[key])
            except (ValueError, TypeError):
                raise WatchError("日期格式应为 YYYY-MM-DD。")
            query = query.filter(**{lookup: value})
    topic = params.get("topic")
    if topic:
        if topic not in {t[0] for t in TOPICS}:
            raise WatchError("未知主题。")
        # JSON array membership is selected without leaking rows across families.
        ids = [
            pk for pk, topics in query.values_list("pk", "topics") if topic in topics
        ]
        query = query.filter(pk__in=ids)
    return query.order_by(F("published_at").desc(nulls_last=True), "-pk")


def clean_url(url):
    import ipaddress

    parsed = urlsplit(str(url))
    host = (parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme not in {"http", "https"}
        or not host
        or parsed.username
        or parsed.password
    ):
        raise WatchError("原文链接必须是公开 HTTP/HTTPS 地址。")
    if host == "localhost" or host.endswith((".local", ".internal", ".localhost")):
        raise WatchError("原文链接不能是内网地址。")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address and not address.is_global:
        raise WatchError("原文链接不能是内网地址。")
    if parsed.port not in (None, 80, 443):
        raise WatchError("原文链接端口不受支持。")
    query = [
        (k, v)
        for k, v in parse_qsl(parsed.query)
        if not k.lower().startswith(("utm_", "spm"))
    ]
    result = urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(query), "")
    )
    if len(result) > 1000:
        raise WatchError("原文链接过长。")
    return result


@transaction.atomic
def ingest(
    source,
    *,
    external_id,
    title,
    summary,
    url,
    published_at=None,
    occurred_at=None,
    status="active",
    official_document=None,
    official_version=None,
    published_precision="time",
):
    source = NewsSource.objects.select_for_update().get(pk=source.pk)
    title = re.sub(r"\s+", " ", title).strip()[:500]
    summary = re.sub(r"\s+", " ", summary).strip()[:1800]
    if len(title) < 4 or status not in {"active", "corrected", "withdrawn"}:
        raise WatchError("材料标题或状态无效。")
    url = clean_url(url)
    identity = str(external_id)[:500] or url
    material = (
        NewsMaterial.objects.filter(source=source, external_id=identity)
        .select_related("current_version")
        .first()
    )
    # Repeated headlines (e.g. each FOMC statement) are distinct dated events.
    # Unknown dates require the same URL; do not guess cross-source identity.
    fingerprint = digest(
        [
            title.casefold(),
            summary.casefold(),
            published_at.date() if published_at else url,
        ]
    )
    content_hash = digest(
        [title, summary, url, published_at, occurred_at, status, published_precision]
    )
    if (
        material
        and material.current_version
        and material.current_version.content_hash == content_hash
    ):
        return material.current_version, False
    if material is None:
        event, _ = InvestmentEvent.objects.get_or_create(
            family=source.family, fingerprint=fingerprint, defaults={"title": title}
        )
        material = NewsMaterial.objects.create(
            source=source,
            external_id=identity,
            event=event,
            official_document=official_document,
        )
    topics, category = classify(title, summary, source.market)
    market = source.market
    if market == "全球" and ("us" in topics) != ("china" in topics):
        market = "美国" if "us" in topics else "中国"
    version = MaterialVersion.objects.create(
        material=material,
        number=(material.current_version.number + 1 if material.current_version else 1),
        title=title,
        summary=summary,
        url=url,
        content_hash=content_hash,
        published_at=published_at,
        occurred_at=occurred_at,
        status=status,
        market=market,
        category=category,
        topics=topics,
        original_chain=(
            f"official:{official_document.pk}" if official_document else fingerprint
        ),
        official_version=official_version,
        published_precision=published_precision if published_at else "unknown",
    )
    material.current_version = version
    material.save(update_fields=["current_version", "updated_at"])
    return version, True


def words(value):
    if (
        not isinstance(value, list)
        or len(value) > 40
        or any(not isinstance(v, str) or len(v) > 100 for v in value)
    ):
        raise WatchError("每组最多 40 个词，每词不超过 100 字。")
    return list(dict.fromkeys(v.strip() for v in value if v.strip()))


def check_revision(dossier, expected):
    if expected != (dossier.current_revision_id or 0):
        raise Conflict("研究判断已变更，请刷新后重试。")


@transaction.atomic
def save_rule(member, dossier_id, values, expected_version):
    writer(member)
    dossier = dossier_for(member, dossier_id, lock=True)
    rule = WatchRule.objects.filter(dossier=dossier).first()
    if expected_version != (rule.version if rule else 0):
        raise Conflict("关注设置已变更，请刷新。")
    if type(values.get("enabled")) is not bool:
        raise WatchError("启停值必须是布尔值。")
    cleaned = {
        key: words(values.get(key, []))
        for key in ("aliases", "topics", "include", "exclude")
    }
    if not cleaned["aliases"] and not cleaned["topics"]:
        raise WatchError("至少填写一个公司别名或关联领域。")
    if rule is None:
        rule = WatchRule(dossier=dossier)
    else:
        rule.version += 1
    for key, value in cleaned.items():
        setattr(rule, key, value)
    rule.enabled = values["enabled"]
    rule.save()
    return rule


@transaction.atomic
def idempotent(member, operation, key, body, action):
    writer(member)
    if not isinstance(key, str) or not 8 <= len(key) <= 100:
        raise WatchError("缺少有效的 Idempotency-Key。")
    FamilyMember.objects.select_for_update().get(pk=member.pk)
    identity = digest(body)
    receipt = OperationReceipt.objects.filter(
        member=member, operation=operation, key=key
    ).first()
    if receipt:
        if receipt.body_hash != identity:
            raise Conflict("同一操作标识不能提交不同内容。")
        return receipt.result
    result = action()
    OperationReceipt.objects.create(
        member=member, operation=operation, key=key, body_hash=identity, result=result
    )
    return result


@transaction.atomic
def associate(
    member,
    dossier_id,
    version_id,
    expected_revision,
    manual=True,
    reason="成员手动关联",
    rule_version=0,
):
    writer(member)
    dossier = dossier_for(member, dossier_id, lock=True)
    check_revision(dossier, expected_revision)
    version = version_for(member, version_id)
    if (
        version.status == "withdrawn"
        or version.material.current_version_id != version.pk
    ):
        raise Conflict("材料已撤回或更新，请查看最新版本。")
    candidate, created = ResearchCandidate.objects.get_or_create(
        dossier=dossier,
        material_version=version,
        revision=dossier.current_revision,
        defaults={"manual": manual, "reason": reason, "rule_version": rule_version},
    )
    if manual and not candidate.manual:
        candidate.manual = True
        candidate.save(update_fields=["manual", "updated_at"])
    return candidate


@transaction.atomic
def annotate(member, event_id, values, expected_version):
    writer(member)
    FamilyMember.objects.select_for_update().get(pk=member.pk)
    event = InvestmentEvent.objects.filter(pk=event_id, family=member.family).first()
    if not event:
        raise Http404
    item = MemberAnnotation.objects.filter(member=member, event=event).first()
    if expected_version != (item.version if item else 0):
        raise Conflict("收藏或备注已更新，请刷新。")
    note = values.get("note", "")
    if not isinstance(note, str) or len(note) > 2000:
        raise WatchError("备注最多 2000 字。")
    for key in ("saved", "read"):
        if type(values.get(key)) is not bool:
            raise WatchError("阅读与收藏状态无效。")
    if item is None:
        item = MemberAnnotation(member=member, event=event)
    else:
        item.version += 1
    item.note = note
    item.saved = values["saved"]
    item.read = values["read"]
    item.save()
    return item


def candidate_stale(candidate):
    return (
        candidate.revision_id != candidate.dossier.current_revision_id
        or candidate.material_version_id
        != candidate.material_version.material.current_version_id
        or candidate.material_version.status == "withdrawn"
    )


@transaction.atomic
def organize_event(member, event_id, target_id, action, reason, expected_updated):
    writer(member)
    if member.role != FamilyMember.ROLE_ADMIN:
        raise PermissionDenied("事件整理由家庭管理员复核。")
    Family.objects.select_for_update().get(pk=member.family_id)
    event = InvestmentEvent.objects.filter(pk=event_id, family=member.family).first()
    target = (
        InvestmentEvent.objects.filter(pk=target_id, family=member.family).first()
        if target_id
        else None
    )
    if not event or (target_id and not target):
        raise Http404
    if event.updated_at.isoformat() != expected_updated:
        raise Conflict("事件关系已更新，请刷新。")
    if (
        action not in {"merge", "unmerge", "followup"}
        or not isinstance(reason, str)
        or not reason.strip()
        or len(reason) > 500
    ):
        raise WatchError("请选择整理方式并填写复核理由。")
    if action != "unmerge" and (not target or target.pk == event.pk):
        raise WatchError("请选择另一条有效事件。")
    if action == "merge":
        if (
            target.merged_into_id
            or InvestmentEvent.objects.filter(merged_into=event).exists()
        ):
            raise WatchError("请先拆开已有分组，避免嵌套合并。")
        event.merged_into = target
    elif action == "unmerge":
        target = event.merged_into
        event.merged_into = None
    else:
        current = target
        seen = {event.pk}
        while current:
            if current.pk in seen:
                raise WatchError("后续关系不能形成循环。")
            seen.add(current.pk)
            current = current.previous
        event.previous = target
    event.merge_reason = reason.strip()
    event.merged_by = member
    event.save()
    EventAction.objects.create(
        event=event, target=target, member=member, action=action, reason=reason.strip()
    )
    return event


def current_evidence(candidate, rows=None):
    """One latest successful analysis batch; keep earlier batches as history."""
    if candidate_stale(candidate):
        return []
    eligible = [e for e in (rows if rows is not None else candidate.evidence.all())
                if e.revision_id == candidate.dossier.current_revision_id]
    if not eligible:
        return []
    key = max(eligible, key=lambda e: e.pk).input_key
    return [e for e in eligible if e.input_key == key]


def recall(dossier):
    rule = WatchRule.objects.filter(dossier=dossier, enabled=True).first()
    if not rule or not dossier.owner.is_active or not is_writer(dossier.owner):
        return 0
    count = 0
    for version in (
        public_versions(dossier.owner).exclude(status="withdrawn").order_by("-pk")[:500]
    ):
        hit, reason = match_rule(rule, version)
        if hit:
            before = ResearchCandidate.objects.filter(
                dossier=dossier,
                material_version=version,
                revision=dossier.current_revision,
            ).exists()
            associate(
                dossier.owner,
                dossier.pk,
                version.pk,
                dossier.current_revision_id or 0,
                manual=False,
                reason=reason,
                rule_version=rule.version,
            )
            count += not before
    return count


@transaction.atomic
def review(member, evidence_id, direction, reason):
    writer(member)
    evidence = (
        ThesisEvidence.objects.select_related(
            "candidate__dossier", "candidate__material_version__material"
        )
        .filter(
            pk=evidence_id,
            candidate__dossier__owner=member,
            candidate__dossier__family=member.family,
        )
        .first()
    )
    if not evidence:
        raise Http404
    if (
        direction not in {"support", "weaken", "mixed", "unknown"}
        or not isinstance(reason, str)
        or not reason.strip()
        or len(reason) > 2000
    ):
        raise WatchError("请选择方向并填写不超过 2000 字的复核理由。")
    if (
        candidate_stale(evidence.candidate)
        or evidence.revision_id != evidence.candidate.dossier.current_revision_id
        or evidence.pk not in {e.pk for e in current_evidence(evidence.candidate)}
    ):
        raise Conflict("证据版本已失效，请使用最新材料重新分析。")
    if direction != "unknown" and (not evidence.quote or not evidence.locator):
        raise WatchError("无引文的候选不能复核为方向性结论。")
    return EvidenceReview.objects.create(
        evidence=evidence, member=member, direction=direction, reason=reason.strip()
    )
