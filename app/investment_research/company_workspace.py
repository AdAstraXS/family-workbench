"""Read-only company workspace on the existing private research dossier."""
from copy import deepcopy

from django.db.models import F, Q

from ai_analysis.models import AiAnalysisRequest
from investment_watch.models import ResearchCandidate
from .models import OfficialResearchContentVersion
from .thesis_analysis import enforce_market_expectation_boundary


def research_history(dossier):
    return AiAnalysisRequest.objects.filter(
        member=dossier.owner, family=dossier.family, module="investment_research",
        analysis_type="thesis_synthesis", scope__dossier_id=dossier.pk,
    ).select_related("result", "provider").order_by("-created_at", "-pk")


def adopted_news(analysis):
    if not analysis:
        return set()
    scope = analysis.scope or {}
    return {item.get("version_id") for item in
            scope.get("news_snapshots", []) + scope.get("baseline_news_snapshots", [])}


def workspace_context(dossier, params):
    history = research_history(dossier)
    histories = list(history[:50])
    latest = history.filter(status=AiAnalysisRequest.STATUS_SUCCESS).first()
    report_is_current = bool(latest and (latest.scope or {}).get("thesis_revision_id") == dossier.current_revision_id)
    result = deepcopy(latest.result.result_json) if latest else None
    if result:
        for index, item in enumerate(result.get("assessments", [])):
            item = enforce_market_expectation_boundary(item)
            item["verdict_label"] = {"supports": "有支持", "weakens": "有反证",
                                     "mixed": "存在不同依据", "unknown": "证据不足"}.get(
                                         item.get("verdict"), "待核对")
            result["assessments"][index] = item
    used_news = adopted_news(latest)
    used_official = {item.get("version_id") for item in
                     (latest.scope or {}).get("sources", [])} if latest else set()
    candidates = ResearchCandidate.objects.filter(
        dossier=dossier, revision_id=dossier.current_revision_id,
        material_version_id=F("material_version__material__current_version_id"),
        material_version__material__source__family_id=dossier.family_id,
    ).exclude(material_version__status="withdrawn").select_related(
        "material_version__material__source").prefetch_related("evidence").order_by("-created_at")
    official = OfficialResearchContentVersion.objects.filter(
        document__security=dossier.security,
    ).select_related("document").defer("raw_gzip", "content_text").order_by("-fetched_at")
    pending_count = candidates.exclude(material_version_id__in=used_news).count()
    official_pending_count = official.exclude(pk__in=used_official).count()
    view = params.get("view", "conclusion")
    if view not in {"conclusion", "changes", "evidence"}:
        view = "conclusion"
    source_type = params.get("source_type", "all")
    assumption = params.get("assumption", "all")
    search = params.get("q", "").strip()[:200]
    if view == "changes":
        candidates = candidates.exclude(material_version_id__in=used_news)
        official = official.exclude(pk__in=used_official)
    if view == "evidence":
        if search:
            candidates = candidates.filter(Q(material_version__title__icontains=search) |
                                           Q(material_version__summary__icontains=search))
            official = official.filter(document__title__icontains=search)
        if assumption != "all":
            candidates = candidates.filter(evidence__assumption_key=assumption).distinct()
            # Official documents are not assigned to a personal hypothesis until cited.
            cited = set()
            for item in ((result or {}).get("assessments", []) if report_is_current else []):
                if f"{item.get('kind')}:{item.get('index')}" == assumption:
                    cited.update(cite.get("version_id") for cite in item.get("citations", [])
                                 if cite.get("kind") != "news")
            official = official.filter(pk__in=cited)
        if source_type == "official":
            candidates = candidates.none()
        elif source_type == "news":
            official = official.none()
    rows = [{"candidate": candidate, "version": candidate.material_version,
             "used": candidate.material_version_id in used_news,
             "relations": list(candidate.evidence.all())} for candidate in candidates[:100]]
    official_rows = [{"version": version, "used": version.pk in used_official}
                     for version in official[:100]]
    revision = dossier.current_revision
    assumptions = ([{"key": f"pillar:{i}", "text": text} for i, text in enumerate(revision.pillars)] +
                   [{"key": f"question:{i}", "text": text} for i, text in enumerate(revision.questions)]) if revision else []
    return {"company_view": view, "research_report": latest, "research_result": result,
            "research_histories": histories, "news_rows": rows, "official_rows": official_rows,
            "pending_news_count": pending_count, "pending_official_count": official_pending_count,
            "assumption_options": assumptions, "source_type": source_type,
            "assumption_filter": assumption, "evidence_search": search,
            "report_is_current": report_is_current}
