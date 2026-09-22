"""Durable, explicit quote refresh for recorded short Put positions."""

import os
from pathlib import Path
import subprocess
import sys
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from family_core.models import Family

from .models import WheelPutQuoteJob
from .open_puts import open_put_rows
from .put_quote_probe import PutQuoteError, fetch_exact_put_quotes, quote_code


def launch_job(job_id):
    kwargs = {"start_new_session": True} if os.name != "nt" else {
        "creationflags": subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
    }
    try:
        subprocess.Popen(
            [sys.executable, "manage.py", "run_wheel_put_quote_job", str(job_id)],
            cwd=Path(__file__).resolve().parent.parent,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True, **kwargs,
        )
    except OSError:
        WheelPutQuoteJob.objects.filter(pk=job_id, status="queued").update(
            status="failed", message="行情进程未能启动。", finished_at=timezone.now(),
        )


def enqueue(family, user):
    with transaction.atomic():
        Family.objects.select_for_update().get(pk=family.pk)
        active = WheelPutQuoteJob.objects.filter(family=family, status__in=["queued", "running"]).order_by("-created_at").first()
        if active and active.expires_at > timezone.now():
            return active
        if active:
            active.status = "interrupted"
            active.message = "先前任务超时；未自动重试，请核对订阅状态。"
            active.finished_at = timezone.now()
            active.save(update_fields=["status", "message", "finished_at", "updated_at"])
        job = WheelPutQuoteJob.objects.create(
            family=family, requested_by=user, expires_at=timezone.now() + timedelta(minutes=5),
        )
        transaction.on_commit(lambda: launch_job(job.pk))
        return job


def run_job(job_id):
    now = timezone.now()
    if not WheelPutQuoteJob.objects.filter(pk=job_id, status="queued", expires_at__gt=now).update(
        status="running", started_at=now,
    ):
        return
    try:
        job = WheelPutQuoteJob.objects.select_related("family", "requested_by").get(pk=job_id)
        if not job.requested_by.is_active or not job.requested_by.is_superuser:
            raise PutQuoteError("申请人的管理员权限已失效。")
        positions = open_put_rows(job.family)
        codes = [quote_code(row["contract"]) for row in positions]
        if not codes or None in codes:
            raise PutQuoteError("未平仓 Put 合约缺少可识别的 Futu 代码。")
        quotes = fetch_exact_put_quotes(codes)
        with transaction.atomic():
            job = WheelPutQuoteJob.objects.select_for_update().get(pk=job_id)
            if job.status != "running" or job.expires_at <= timezone.now():
                raise PutQuoteError("任务已中断或超过保存期限。")
            job.status = "saved"
            job.quotes = quotes
            job.message = f"已查询 {len(quotes)} 张真实未平仓 Put；临时订阅额度已恢复。"
            job.finished_at = timezone.now()
            job.save(update_fields=["status", "quotes", "message", "finished_at", "updated_at"])
    except PutQuoteError as exc:
        WheelPutQuoteJob.objects.filter(pk=job_id, status="running").update(
            status="failed", message=str(exc), finished_at=timezone.now(),
        )
    except Exception:
        WheelPutQuoteJob.objects.filter(pk=job_id, status="running").update(
            status="failed", message="Futu 持仓行情查询失败；订阅清理状态需核对。",
            finished_at=timezone.now(),
        )
