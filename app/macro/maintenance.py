"""Explicit background maintenance. Page views never discover or fetch reports."""
from contextlib import contextmanager
from datetime import timedelta

from django.db import connection
from django.utils import timezone

from .adapters import SourceError, digest
from .discovery import CATALOGUES, discover
from .models import MacroMaintenanceRun, MacroOfficialReport
from .registry import GROUPS, OFFICIAL_GROUPS
from .services import fetch_page, fetch_source, import_group, prepare

LOCK_ID = 736204081


@contextmanager
def maintenance_lock():
    # A session lock is released by PostgreSQL even when the worker is killed.
    if connection.vendor != "postgresql":
        raise SourceError("自动更新需要 PostgreSQL 会话锁")
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [LOCK_ID])
        acquired = cursor.fetchone()[0]
    if not acquired:
        raise SourceError("已有宏观维护任务运行，本次未启动")
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s)", [LOCK_ID])


def recent_start(today):
    month = today.replace(day=1)
    previous = (month - timedelta(days=1)).replace(day=1)
    return (previous - timedelta(days=1)).replace(day=1)


def update_official(start, end, *, write=False, pages=5, groups=None, reader=None, fetcher=None):
    summary = {"reports": [], "failures": [], "start": str(start), "end": str(end)}
    for group in groups or CATALOGUES:
        try:
            kwargs = {"pages": pages, "reader": reader or fetch_page}
            reports = discover(group, start, end, **kwargs)
        except Exception as exc:
            error = str(exc) if isinstance(exc, SourceError) else f"官方目录读取失败（{type(exc).__name__}）"
            summary["failures"].append({"group": group, "error": error})
            continue
        for report in reports:
            record = None
            if write:
                record, _ = MacroOfficialReport.objects.get_or_create(url=report.url, defaults={"group": group, "title": report.title})
            try:
                payload = (fetcher or fetch_source)(group, report.url)
                points = prepare(group, payload)
                if {p.period for p in points} != {report.period}:
                    raise SourceError("目录标题与正文统计期不一致")
                if not all(p.release_date for p in points):
                    raise SourceError("官方发布日期未核验，暂不自动导入")
                result = import_group(group, write=write, url=report.url, fetcher=lambda *_: payload)
                summary["reports"].append({"group": group, "url": report.url, "title": report.title, **result})
                if record:
                    record.title = report.title
                    record.period_date, record.release_date = report.period, points[0].release_date
                    record.content_hash = digest(payload["text"])
                    record.status, record.error, record.summary = "success", "", result
                    record.checked_at = record.imported_at = timezone.now()
                    record.save()
            except SourceError as exc:
                summary["failures"].append({"group": group, "url": report.url, "error": str(exc)})
                if record:
                    record.status, record.error, record.checked_at = "failed", str(exc)[:500], timezone.now()
                    record.save(update_fields=["status", "error", "checked_at"])
    return summary


def maintain(mode, *, write=False, start=None, end=None, pages=5, groups=None):
    with maintenance_lock():
        if write:
            # Exclusive lock proves no other managed run is alive. Preserve evidence of interruption.
            MacroMaintenanceRun.objects.filter(status="running").update(
                status="interrupted", finished_at=timezone.now(), error="上次任务未完成；已释放会话锁，本次重新检查来源。")
        run = MacroMaintenanceRun.objects.create(mode=mode) if write else None
        summary = {"failures": [], "mode": mode}
        try:
            if mode in {"official", "all"}:
                today = timezone.localdate()
                result = update_official(start or recent_start(today), end or today, write=write, pages=pages, groups=groups)
                summary["official"] = result
                summary["failures"] += result["failures"]
            if mode in {"structured", "all"}:
                summary["structured"] = {}
                for group in sorted(set(GROUPS) - OFFICIAL_GROUPS):
                    try:
                        summary["structured"][group] = import_group(group, write=write)
                    except SourceError as exc:
                        summary["failures"].append({"group": group, "error": str(exc)})
            if mode in {"calendar", "all"}:
                from .calendar_maintenance import refresh_calendars
                summary["calendar"] = refresh_calendars(write=write, reader=fetch_page)
                summary["failures"] += summary["calendar"]["failures"]
            if run:
                run.summary, run.finished_at = summary, timezone.now()
                run.status = "failed" if summary["failures"] else "success"
                run.error = f'{len(summary["failures"])} 项来源未完成；详见来源记录' if summary["failures"] else ""
                run.save()
            return summary
        except Exception as exc:
            if run:
                run.status, run.finished_at = "failed", timezone.now()
                run.error = str(exc)[:500] if isinstance(exc, SourceError) else f"任务异常（{type(exc).__name__}），请查看日志"
                run.save(update_fields=["status", "finished_at", "error"])
            raise
