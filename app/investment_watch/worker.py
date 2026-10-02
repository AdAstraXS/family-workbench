import uuid
from datetime import timedelta
from django.conf import settings
from django.db import transaction
from django.db.models import F
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
    WatchPipelineState,
    ScreeningBatch,
    BodySnapshot,
    BodyAttempt,
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
    from .events import canonical_candidate

    result = []
    seen = set()
    for candidate in (
        ResearchCandidate.objects.filter(
            dossier=dossier, revision=dossier.current_revision
        )
        .select_related("dossier__current_revision", "material_version__material")
        .order_by("-manual", "-pk")
    ):
        if candidate_stale(candidate):
            continue
        candidate = canonical_candidate(candidate)
        key = analysis_key(candidate, provider)
        if key in seen:
            continue
        seen.add(key)
        # Reserved or failed calls have unknown costs and are never silently repeated.
        if (
            not ThesisEvidence.objects.filter(input_key=key).exists()
            and not BudgetReceipt.objects.filter(input_key=key).exists()
        ):
            result.append(candidate)
    return result


def queue_run(dossier):
    from .models import MaterialRelation
    from .analysis import PROMPT_VERSION

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
            PROMPT_VERSION,
            bool(getattr(settings, "INVESTMENT_WATCH_BODY_ENABLED", False)),
            pipeline_progress(dossier) if getattr(settings, "INVESTMENT_WATCH_BODY_ENABLED", False) else None,
            MaterialRelation.objects.filter(
                source__material__source__family=dossier.family
            )
            .order_by("-pk")
            .values_list("pk", flat=True)
            .first(),
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


def pipeline_progress(dossier):
    from .body_capture import china_day
    return [str(china_day()),
            ScreeningBatch.objects.filter(dossier=dossier).order_by("-pk").values_list("pk", flat=True).first(),
            BodySnapshot.objects.filter(material_version__researchcandidate__dossier=dossier).order_by("-pk").values_list("pk", flat=True).first()]


def process_pipeline(dossier):
    from .screening import screen_candidates, ready_candidates, authorized_provider
    from .body_capture import capture_body, BodyQuotaExhausted
    candidates = ResearchCandidate.objects.filter(dossier=dossier, revision=dossier.current_revision).select_related(
        "dossier__owner__family", "dossier__current_revision", "dossier__security", "material_version__material__source").order_by("-material_version__found_at", "-pk")
    screened = 0
    for _ in range(4):
        batch_count = screen_candidates(dossier, candidates)
        screened += batch_count
        if not batch_count:
            break
    else:
        # Rank the complete visible pool before spending scarce body slots. Continue
        # oversized pools next scheduler tick rather than favoring the first batch.
        return 0, f"本轮初筛 {screened} 条；继续筛选余下候选后再按优先级读正文。", False
    provider = authorized_provider(dossier)
    count = 0
    analyzed = 0
    messages = []
    deferred = False
    for candidate in ready_candidates(dossier):
        attempt = BodyAttempt.objects.filter(family=dossier.family, security=dossier.security,
            material_version=candidate.material_version).first()
        if attempt and attempt.status in {"reserved", "failed"}:
            continue
        key = analysis_key(candidate, provider)
        if BodySnapshot.objects.filter(material_version=candidate.material_version).exists() and (
                ThesisEvidence.objects.filter(input_key=key).exists() or BudgetReceipt.objects.filter(input_key=key).exists()):
            continue
        try:
            capture_body(candidate)
            key = analysis_key(candidate, provider)
            if not ThesisEvidence.objects.filter(input_key=key).exists() and not BudgetReceipt.objects.filter(input_key=key).exists():
                count += analyze_candidate(candidate.pk)
                analyzed += 1
        except BodyQuotaExhausted:
            deferred = True
        except WatchError as exc:
            messages.append(str(exc))
        if analyzed >= 3:
            break
    return count, (f"本轮初筛 {screened} 条，正文分析 {analyzed} 篇。" + (messages[0] if messages else
        "今日正文名额已满，余下重要候选等待后续检查。" if deferred else "仅选择重要事件；没有合适候选时不抓取。"))[:500], bool(messages)


def run_cycle(family, *, collect=True, analyze=True, limit=3):
    token = acquire(family)
    if not token:
        return {"status": "busy"}
    try:
        results = []
        body_enabled = getattr(settings, "INVESTMENT_WATCH_BODY_ENABLED", False)
        if body_enabled:
            # Establish the watermark before collecting this cycle's new materials.
            WatchPipelineState.objects.get_or_create(family=family)
        if collect:
            due_sources = [
                source
                for source in NewsSource.objects.filter(family=family, enabled=True)
                .exclude(adapter="official")
                .order_by(F("last_checked_at").asc(nulls_first=True), "pk")
                if not source.last_checked_at
                or timezone.now() - source.last_checked_at
                >= timedelta(minutes=max(15, source.interval_minutes))
            ]
            for source in due_sources[:8]:
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
                    if body_enabled:
                        count, run.message, had_errors = process_pipeline(run.dossier)
                        run.status = "blocked" if had_errors else "completed"
                        blocked += int(had_errors)
                    else:
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
