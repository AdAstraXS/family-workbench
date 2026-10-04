"""Shared resource guard for existing management-command jobs.

Business consent, idempotency and recovery remain with each module. No automatic
retry: a rejected/failed task stays visible in its existing business record.
"""
from contextlib import contextmanager
import logging
import signal
import threading
import time

from django.apps import apps
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.utils import timezone

logger = logging.getLogger("workbench.jobs")
LOCK_NAMESPACE = 1464222282


class JobCapacityError(Exception):
    pass


class JobDeadline(BaseException):
    pass


@contextmanager
def job_slot():
    # Production PostgreSQL coordinates every web worker and DSM process.
    if connection.vendor != "postgresql":
        yield
        return
    slot = None
    with connection.cursor() as cursor:
        for candidate in range(max(1, int(getattr(settings, "BACKGROUND_JOB_SLOTS", 2)))):
            cursor.execute("SELECT pg_try_advisory_lock(%s, %s)", [LOCK_NAMESPACE, candidate])
            if cursor.fetchone()[0]:
                slot = candidate
                break
    if slot is None:
        raise JobCapacityError("后台任务繁忙，本次未执行；请稍后主动重试。")
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s, %s)", [LOCK_NAMESPACE, slot])


def record_failure(command, options, message, *, started):
    key = options.get("request_id")
    if key is not None:
        model = apps.get_model("ai_analysis", "AiAnalysisRequest")
        model.objects.filter(pk=key, status__in=["pending", "running"] if started else ["pending"]).update(
            status="failed", error_message=message, finished_at=timezone.now())
        return
    models = {
        "run_wheel_analysis_job": ("option_wheel", "WheelAnalysisJob"),
        "run_wheel_position_scan": ("option_wheel", "WheelPositionScanJob"),
        "run_wheel_put_quote_job": ("option_wheel", "WheelPutQuoteJob"),
        "run_company_acquisition": ("investment_research", "CompanyAcquisitionJob"),
    }
    if command not in models or options.get("job_id") is None:
        return
    model = apps.get_model(*models[command])
    values = {"status": "failed", "finished_at": timezone.now()}
    if command == "run_company_acquisition":
        with transaction.atomic():
            job = model.objects.select_for_update().filter(pk=options["job_id"],
                status__in=["queued", "running"] if started else ["queued"]).first()
            if job:
                values["items"] = [*(job.items or []),
                    {"title": "后台执行", "status": "failed", "message": message}]
                model.objects.filter(pk=job.pk).update(**values)
        return
    else:
        values["message"] = message
    model.objects.filter(pk=options["job_id"], status__in=["queued", "running"] if started else ["queued"]).update(**values)


class BoundedJobCommand(BaseCommand):
    def execute(self, *args, **options):
        command = self.__module__.rsplit(".", 1)[-1]
        started_at = time.monotonic()
        started = False
        alarm = hasattr(signal, "SIGALRM") and threading.current_thread() is threading.main_thread()
        old_handler = None
        outcome = "failed"
        try:
            with job_slot():
                started = True
                if alarm:
                    old_handler = signal.getsignal(signal.SIGALRM)
                    def deadline(signum, frame):
                        raise JobDeadline()
                    signal.signal(signal.SIGALRM, deadline)
                    signal.alarm(max(1, int(getattr(settings, "BACKGROUND_JOB_TIMEOUT_SECONDS", 900))))
                logger.info("job_start command=%s", command)
                result = super().execute(*args, **options)
                outcome = "finished"
                return result
        except (JobCapacityError, JobDeadline) as exc:
            message = str(exc) if isinstance(exc, JobCapacityError) else "后台任务超过运行时限，已停止；请核对结果后主动重试。"
            record_failure(command, options, message, started=started)
            raise CommandError(message) from None
        except Exception as exc:
            logger.error("job_error command=%s error_type=%s", command, type(exc).__name__)
            record_failure(command, options, "后台执行异常，请在任务记录中核对；未自动重试。", started=started)
            raise CommandError("后台任务未完成，请在网页任务记录中核对状态。") from None
        finally:
            if alarm and old_handler is not None:
                signal.alarm(0)
                signal.signal(signal.SIGALRM, old_handler)
            logger.info("job_end command=%s outcome=%s elapsed_ms=%d", command, outcome,
                        (time.monotonic() - started_at) * 1000)

