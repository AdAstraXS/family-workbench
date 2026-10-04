"""Read-only state labels for daily reading."""
from datetime import timedelta
from django.utils import timezone
from django.db.models import Count
from .models import BodyAttempt, BodySnapshot, WatchConsent, WatchRule
from .profiles import profile, relevance
from .screening import screening_key, chosen


def state_cache(candidates):
    ids = {c.dossier_id for c in candidates}
    versions = {c.material_version_id for c in candidates}
    from .body_capture import china_day
    families = {c.dossier.family_id for c in candidates}
    return {
        "rules": {r.dossier_id: r for r in WatchRule.objects.filter(dossier_id__in=ids)},
        "consents": {c.dossier_id: c for c in WatchConsent.objects.filter(dossier_id__in=ids).select_related("provider")},
        "bodies": dict(BodySnapshot.objects.filter(material_version_id__in=versions).values_list("material_version_id", "method")),
        "attempts": {(a.security_id, a.material_version_id): a.status for a in BodyAttempt.objects.filter(
            family_id__in=families, material_version_id__in=versions)},
        "used": {row["security_id"]: row["count"] for row in BodyAttempt.objects.filter(
            family_id__in=families, day=china_day()).values("security_id").annotate(count=Count("pk"))},
    }


def reading_state(candidate, cache=None):
    cache = cache or state_cache([candidate])
    consent = cache["consents"].get(candidate.dossier_id)
    company = profile(candidate.dossier, cache["rules"].get(candidate.dossier_id))
    screening = None
    if consent:
        key = screening_key(candidate, consent.provider, company["rule_version"])
        screening = next((s for s in candidate.screenings.all() if s.input_key == key), None)
    candidate.latest_screening = screening
    relevant = screening.relevance if screening and screening.relevance != "unknown" else relevance(candidate.material_version, company)
    candidate.relevance_label = {"direct": "公司或产品相关", "industry": "行业关联", "unknown": "关联待核对"}[relevant]
    candidate.reading_important = chosen(candidate, screening)
    candidate.reading_recent = candidate.material_version.found_at >= timezone.now() - timedelta(hours=72)
    body = cache["bodies"].get(candidate.material_version_id)
    attempt = cache["attempts"].get((candidate.dossier.security_id, candidate.material_version_id))
    if body:
        candidate.reading_label = "已保存 RSS 正文" if body == "rss-content" else "已获取正文"
    elif attempt:
        candidate.reading_label = "正文获取失败" if attempt == "failed" else "正文请求已预留"
    elif candidate.reading_important:
        candidate.reading_label = ("仅标题与摘要 · 今日名额已满" if cache["used"].get(candidate.dossier.security_id, 0) >= 3
                                   else "仅标题与摘要 · 等待正文处理")
    elif screening and screening.duplicate_of_id:
        candidate.reading_label = "同事件报道 · 暂不重复抓取"
    elif screening and screening.batch.status == "completed":
        candidate.reading_label = "仅标题与摘要 · 暂不抓取"
    else:
        candidate.reading_label = "仅标题与摘要 · 待筛选"
    return candidate
