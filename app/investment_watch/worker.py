import uuid
from datetime import timedelta
from django.db import transaction
from django.utils import timezone
from family_core.models import Family
from .models import (
    WorkerLease,
    NewsSource,
    WatchRun,
    ResearchCandidate,
    WatchConsent,
    ThesisEvidence,
    BudgetReceipt,
)
from .collection import collect_source, import_official
from .services import recall, WatchError, digest, candidate_stale
from .analysis import analyze_candidate, analysis_key, provider_signature
from investment_research.models import ResearchDossier
from investment_research.permissions import is_writer


@transaction.atomic
def acquire(family):
    Family.objects.select_for_update().get(pk=family.pk)
    key = f"investment-watch:{family.pk}"
    lease = WorkerLease.objects.filter(key=key).first()
    now = timezone.now()
    if lease and lease.expires_at > now:
        return None
    token = uuid.uuid4().hex
    WorkerLease.objects.update_or_create(
        key=key, defaults={"token": token, "expires_at": now + timedelta(minutes=20)}
    )
    return token


def pending_candidates(dossier, provider):
    result = []
    for candidate in (
        ResearchCandidate.objects.filter(
            dossier=dossier, revision=dossier.current_revision
        )
        .select_related("dossier__current_revision", "material_version__material")
        .order_by("-manual", "-pk")
    ):
        if candidate_stale(candidate):
            continue
        key = analysis_key(candidate, provider)
        # Reserved or failed calls have unknown costs and are never silently repeated.
        if (
            not ThesisEvidence.objects.filter(input_key=key).exists()
            and not BudgetReceipt.objects.filter(input_key=key).exists()
        ):
            result.append(candidate)
    return result


def queue_run(dossier):
    consent = (
        WatchConsent.objects.filter(dossier=dossier, active=True)
        .select_related("provider")
        .first()
    )
    versions = list(
        ResearchCandidate.objects.filter(
            dossier=dossier, revision=dossier.current_revision
        )
        .order_by("pk")
        .values_list("pk", "material_version_id", "rule_version")
    )
    key = digest(
        [
            dossier.owner_id,
            dossier.pk,
            dossier.current_revision_id,
            versions,
            provider_signature(consent.provider) if consent else "no-consent",
        ]
    )
    run, _ = WatchRun.objects.get_or_create(
        input_key=key,
        defaults={"dossier": dossier, "revision": dossier.current_revision},
    )
    if run.status == "blocked":
        run.status = "queued"
        run.save(update_fields=["status", "updated_at"])
    return run


def run_cycle(family, *, collect=True, analyze=True, limit=3):
    token = acquire(family)
    if not token:
        return {"status": "busy"}
    try:
        results = []
        if collect:
            for source in NewsSource.objects.filter(
                family=family, enabled=True
            ).exclude(adapter="official")[:8]:
                results.append({"source": source.key, **collect_source(source)})
        import_official(family)
        recalled = 0
        for dossier in ResearchDossier.objects.filter(
            family=family, owner__is_active=True
        ).select_related("owner__family", "current_revision"):
            recalled += recall(dossier)
            if (
                analyze
                and WatchConsent.objects.filter(dossier=dossier, active=True).exists()
            ):
                queue_run(dossier)
        # Reclaim interrupted runs only after acquiring the expired family lease.
        WatchRun.objects.filter(dossier__family=family, status="running").update(
            status="queued", message="上轮中断，继续未付费的候选。"
        )
        completed = 0
        blocked = 0
        # Explicitly queued requests, not every item in the broad pool.
        for run in WatchRun.objects.filter(
            dossier__family=family, status="queued"
        ).select_related("dossier__owner", "dossier__current_revision")[
            : max(1, min(limit, 3))
        ]:
            if run.revision_id != run.dossier.current_revision_id:
                run.status = "stale"
                run.message = "判断已更新，请重新发起。"
            elif not analyze:
                continue
            else:
                run.status = "running"
                run.save(update_fields=["status", "updated_at"])
                try:
                    count = 0
                    if not is_writer(run.dossier.owner):
                        raise WatchError("档案成员已停用或变为只读，停止后台分析。")
                    consent = (
                        WatchConsent.objects.filter(dossier=run.dossier, active=True)
                        .select_related("provider")
                        .first()
                    )
                    if not consent:
                        raise WatchError("尚未授权个人假设分析。")
                    candidates = pending_candidates(run.dossier, consent.provider)
                    for candidate in candidates[:3]:
                        count += analyze_candidate(candidate.pk)
                    run.status = "queued" if len(candidates) > 3 else "completed"
                    run.message = (
                        "已处理本批，余下候选将在下一轮继续。"
                        if len(candidates) > 3
                        else "本轮检查结束；失败或用量未知的调用不自动重复。"
                    )
                    completed += 1
                except WatchError as exc:
                    run.status = "blocked"
                    run.message = str(exc)[:500]
                    blocked += 1
                run.count += count
            run.save(update_fields=["status", "message", "count", "updated_at"])
        return {
            "status": "done",
            "sources": results,
            "recalled": recalled,
            "runs": completed,
            "blocked": blocked,
        }
    finally:
        WorkerLease.objects.filter(
            key=f"investment-watch:{family.pk}", token=token
        ).delete()
