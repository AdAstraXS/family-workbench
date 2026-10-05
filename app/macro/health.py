from datetime import timedelta

from django.db.models import Count, Max
from django.utils import timezone

from .models import MacroMaintenanceRun, MacroOfficialReport, MacroObservation


def update_health():
    now = timezone.now()
    jobs = []
    for mode, label, hours in [("official", "官方报告发现与更新", 20), ("structured", "中美历史接口更新", 36), ("calendar", "官方发布日历核验", 36)]:
        run = MacroMaintenanceRun.objects.filter(mode__in=[mode, "all"]).order_by("-started_at").first()
        if not run:
            state, message = "pending", "尚无执行记录；不能确认定时任务已启用"
        elif run.status == "running":
            state, message = ("warning", "任务运行时间较长，请检查日志") if now - run.started_at > timedelta(hours=2) else ("running", "正在检查来源")
        elif run.status != "success":
            state, message = "warning", run.error or "最近一次任务未完成"
        elif now - run.finished_at > timedelta(hours=hours):
            state, message = "warning", "超过预期检查间隔；请检查 NAS 定时任务"
        else:
            state, message = "success", "最近一次检查成功；实际统计期见历史覆盖表"
        jobs.append({"mode": mode, "name": label, "run": run, "state": state, "message": message,
                     "failures": run.summary.get("failures", [])[:10] if run else []})
    counts = MacroObservation.objects.aggregate(total=Count("id"), released=Count("release_date"))
    reports = MacroOfficialReport.objects.filter(status="failed").order_by("-checked_at")[:10]
    return {"jobs": jobs, "reports": reports, "dates": counts}
