"""Explicit background scan of one recorded option position."""

import os
from pathlib import Path
import subprocess
import sys
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from family_core.models import Family

from .models import WheelPositionScanJob
from .position_evidence import participating_accounts
from .position_scan import fetch_position_scan
from .put_quote_probe import PutQuoteError


def launch_job(job_id):
    kwargs = {"start_new_session": True} if os.name != "nt" else {
        "creationflags": subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
    }
    try:
        subprocess.Popen(
            [sys.executable, "manage.py", "run_wheel_position_scan", str(job_id)],
            cwd=Path(__file__).resolve().parent.parent,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True, **kwargs,
        )
    except OSError:
        WheelPositionScanJob.objects.filter(pk=job_id, status="queued").update(
            status="failed", message="行情进程未能启动。", finished_at=timezone.now(),
        )


def enqueue(family, user, position, target_expiration):
    with transaction.atomic():
        Family.objects.select_for_update().get(pk=family.pk)
        active = WheelPositionScanJob.objects.filter(family=family, status__in=["queued", "running"]).first()
        if active and active.expires_at > timezone.now():
            return active
        if active:
            active.status = "interrupted"
            active.message = "先前任务已超时；未自动重试。"
            active.finished_at = timezone.now()
            active.save(update_fields=["status", "message", "finished_at", "updated_at"])
        job = WheelPositionScanJob.objects.create(
            family=family, requested_by=user, position=position,
            target_expiration=target_expiration, expires_at=timezone.now() + timedelta(minutes=8),
        )
        transaction.on_commit(lambda: launch_job(job.pk))
        return job


def run_job(job_id):
    now = timezone.now()
    if not WheelPositionScanJob.objects.filter(pk=job_id, status="queued", expires_at__gt=now).update(
        status="running", started_at=now,
    ):
        return
    try:
        job = WheelPositionScanJob.objects.select_related(
            "requested_by", "position__security__option_contract__underlying",
        ).get(pk=job_id)
        if not job.requested_by.is_active or not job.requested_by.is_superuser:
            raise PutQuoteError("申请人的管理员权限已失效。")
        allowed_accounts = {account.pk for account in participating_accounts(job.family).values()}
        if (job.position.account_id not in allowed_accounts or job.position.quantity == 0
                or job.position.account.bank_account.family_id != job.family_id):
            raise PutQuoteError("持仓已变化或不属于当前家庭。")
        result = fetch_position_scan(job.position, job.target_expiration)
        with transaction.atomic():
            job = WheelPositionScanJob.objects.select_for_update().get(pk=job_id)
            if job.status != "running" or job.expires_at <= timezone.now():
                raise PutQuoteError("任务已中断或超过保存期限。")
            job.status = "saved"
            job.result = result
            job.message = f"已查询该到期日 {result['sample_count']} 张候选合约；临时订阅已清理。"
            job.finished_at = timezone.now()
            job.save(update_fields=["status", "result", "message", "finished_at", "updated_at"])
    except PutQuoteError as exc:
        WheelPositionScanJob.objects.filter(pk=job_id, status="running").update(
            status="failed", message=str(exc), finished_at=timezone.now(),
        )
    except Exception:
        WheelPositionScanJob.objects.filter(pk=job_id, status="running").update(
            status="failed", message="Futu 持仓比较失败；订阅清理状态需核对。", finished_at=timezone.now(),
        )
