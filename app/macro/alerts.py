"""Idempotent on-site alerts; GET never creates or resolves notifications."""
import re
from datetime import date, timedelta

from django.db import connection, transaction
from django.db.models import Max
from django.utils import timezone

from .calendar import current_schedule
from .health import update_health
from .models import MacroAlert, MacroObservation, MacroPublication, MacroOperationsSnapshot
from .presentation import shifted
from .publications import MONTHS
from .registry import SERIES


def expected_period(event, spec):
    match = re.search(r"(" + "|".join(MONTHS) + r")\s+(20\d{2})", event["period"], re.I)
    if match:
        return date(int(match[2]), MONTHS[match[1].lower()], 1)
    if spec.country == "US" and event.get("agency") == "bls" and spec.frequency == "月度":
        # The BLS ICS omits the reference month. This is only a planned
        # freshness expectation, never an actual release-date annotation.
        return shifted(event["day"].replace(day=1), -1)
    if spec.country != "CN":
        return None
    release = event["day"].replace(day=1)
    if spec.frequency == "季度":
        prior = shifted(release, -3)
        return prior.replace(month=(prior.month - 1) // 3 * 3 + 1)
    return release if spec.code.startswith("PMI_") else shifted(release, -1)


def freshness_issues(now=None):
    now = now or timezone.now()
    specs = {(s.country, s.code): s for s in SERIES}
    expected = {}
    grace = timedelta(hours=48)
    for event in current_schedule()["events"]:
        if not now - timedelta(days=75) <= event["when"] <= now - grace:
            continue
        for code in event["codes"]:
            spec = specs.get((event["country"], code))
            if not spec:
                continue
            period = expected_period(event, spec)
            key = (spec.country, code)
            if period and (key not in expected or period > expected[key][0]):
                expected[key] = (period, "已过官方计划发布日及48小时等待期", event["source"]["url"])
    for publication in MacroPublication.objects.filter(first_seen_at__lte=now - grace).order_by("period_date"):
        for code in publication.codes:
            key = (publication.country, code)
            if key not in expected or publication.period_date >= expected[key][0]:
                expected[key] = (publication.period_date, "官方发布稿已核验，数据仍未跟上", publication.source_url)
    latest = {(row["mapping__indicator__country"], row["mapping__indicator__code"]): row["latest"]
        for row in MacroObservation.objects.filter(value__isnull=False, mapping__indicator__is_active=True)
            .values("mapping__indicator__country", "mapping__indicator__code").annotate(latest=Max("period_date"))}
    return [{"key": "late:" + country + ":" + code, "title": specs[(country, code)].name + "可能漏更",
        "message": f"预期统计期 {period}；已入库最新有效值 {latest.get((country, code)) or '暂无'}。{reason}。",
        "details": {"country": country, "code": code, "expected": str(period), "latest": str(latest.get((country, code))), "url": url}}
        for (country, code), (period, reason, url) in expected.items()
        if latest.get((country, code)) is None or latest[(country, code)] < period]


@transaction.atomic
def synchronize_alerts(now=None):
    now = now or timezone.now()
    # Serialize monitoring and source tasks before resolving or creating alerts.
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [736204082])
    issues = freshness_issues(now)
    for job in update_health()["jobs"]:
        if job["state"] == "warning":
            issues.append({"key": "job:" + job["mode"], "title": job["name"] + "需检查",
                "message": job["message"], "details": {"failures": job["failures"]}})
    operations = MacroOperationsSnapshot.objects.filter(pk=1).first()
    if operations:
        if now - operations.checked_at > timedelta(hours=2):
            issues.append({"key": "host:stale", "title": "NAS定时核验未更新", "message": "超过两小时未收到NAS核验记录，请检查运行监控任务。", "details": {}})
        for task in operations.payload.get("tasks", []):
            if not task["enabled"]:
                issues.append({"key": "host:" + str(task["id"]), "title": "宏观定时任务配置需检查", "message": task["name"] + "未确认启用或运行频率不符。", "details": {"task": task["id"]}})
    keys = {i["key"] for i in issues}
    MacroAlert.objects.filter(resolved_at__isnull=True).exclude(key__in=keys).update(resolved_at=now)
    for issue in issues:
        alert, _ = MacroAlert.objects.get_or_create(key=issue["key"], resolved_at=None,
            defaults={**issue, "last_seen_at": now})
        alert.title, alert.message, alert.details, alert.last_seen_at = issue["title"], issue["message"], issue["details"], now
        alert.save(update_fields=["title", "message", "details", "last_seen_at"])
    return {"active": len(keys)}


def unread_count(request):
    if not request.user.is_authenticated:
        return {}
    return {"macro_unread_alerts": MacroAlert.objects.filter(resolved_at__isnull=True)
            .exclude(macroalertread__user=request.user).count()}
