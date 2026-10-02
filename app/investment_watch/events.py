"""Conservative suggestions and explicitly reviewed, version-bound evidence relations."""

from difflib import SequenceMatcher
from django.db import transaction
from django.db.models import F
from django.core.exceptions import PermissionDenied
from family_core.models import FamilyMember, Family
from .models import MaterialRelation, MaterialVersion
from .services import public_versions, version_for, writer, WatchError, Conflict


def latest_relation(version):
    return (
        version.relation_history.select_related("target__material", "member")
        .order_by("-pk")
        .first()
    )


def canonical_version(version):
    seen = set()
    while version.pk not in seen:
        seen.add(version.pk)
        relation = latest_relation(version)
        if not relation:
            exact = (
                MaterialVersion.objects.filter(
                    material__source__family_id=version.material.source.family_id,
                    material__current_version_id=F("pk"),
                    original_chain=version.original_chain,
                    status="active",
                    pk__lt=version.pk,
                )
                .select_related("material__source")
                .order_by("pk")
                .first()
            )
            if exact:
                version = exact
                continue
            break
        if relation.kind != "duplicate" or not relation.target:
            break
        target = relation.target
        if (
            target.material.current_version_id != target.pk
            or target.status == "withdrawn"
        ):
            break
        version = target
    return version


@transaction.atomic
def relate(member, version_id, target_id, kind, reason, expected):
    writer(member)
    if member.role != FamilyMember.ROLE_ADMIN:
        raise PermissionDenied("仅管理员可整理共享材料关系。")
    Family.objects.select_for_update().get(pk=member.family_id)
    source = version_for(member, version_id)
    # Serialise decisions about the same version; history is append-only.
    type(source).objects.select_for_update().get(pk=source.pk)
    latest = latest_relation(source)
    if (latest.pk if latest else 0) != expected:
        raise Conflict("材料关系已更新，请刷新后复核。")
    if source.material.current_version_id != source.pk or source.status == "withdrawn":
        raise Conflict("请使用最新有效材料整理关系。")
    if (
        kind not in {"duplicate", "followup", "conflict", "independent"}
        or not reason.strip()
        or len(reason) > 500
    ):
        raise WatchError("请选择关系并填写不超过 500 字的复核理由。")
    target = None if kind == "independent" else version_for(member, target_id)
    if target and (
        target.pk == source.pk
        or target.material.current_version_id != target.pk
        or target.status == "withdrawn"
    ):
        raise WatchError("请选择另一份有效材料作为依据；新版本需要重新复核。")
    if target and kind == "duplicate" and canonical_version(target).pk == source.pk:
        raise WatchError("重复关系不能形成循环，请先撤销已有关系。")
    return MaterialRelation.objects.create(
        source=source, target=target, kind=kind, reason=reason.strip(), member=member
    )


def suggestions(member, version):
    rows = []
    for other in (
        public_versions(member)
        .exclude(status="withdrawn")
        .filter(pk__lt=version.pk)
        .order_by("-pk")[:300]
    ):
        score = SequenceMatcher(
            None, version.title.casefold(), other.title.casefold()
        ).ratio()
        if score >= 0.48 or other.original_chain == version.original_chain:
            rows.append((score, other))
    return [v for _, v in sorted(rows, key=lambda row: row[0], reverse=True)[:5]]


def canonical_candidate(candidate):
    from .services import associate

    version = canonical_version(candidate.material_version)
    if version.pk == candidate.material_version_id:
        return candidate
    return associate(
        candidate.dossier.owner,
        candidate.dossier_id,
        version.pk,
        candidate.revision_id or 0,
        manual=False,
        reason="精确内容去重或人工复核后，使用共用的原始证据分析",
        rule_version=candidate.rule_version,
    )
