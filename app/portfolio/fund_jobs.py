import os
import subprocess
import sys
from pathlib import Path
from datetime import timedelta
from django.db import transaction
from django.utils import timezone
from family_core.models import Family, FamilyMember
from family_core.job_runtime import JobDeadline
from .fund_models import FundDcaPlan, FundReference, FundRefreshRun
from .fund_data import refresh_fund


def launch_job(job_id):
    flags = {"start_new_session": True} if os.name != "nt" else {
        "creationflags": subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS}
    try:
        subprocess.Popen([sys.executable, "manage.py", "run_fund_refresh_job", str(job_id)],
            cwd=Path(__file__).resolve().parent.parent, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=None, close_fds=True, **flags)
    except OSError:
        FundRefreshRun.objects.filter(pk=job_id, status="queued").update(
            status="failed", message="更新进程未能启动，请稍后重试。", finished_at=timezone.now())


def enqueue(family, user, fund_ids, *, launch=True):
    with transaction.atomic():
        Family.objects.select_for_update().get(pk=family.pk)
        now = timezone.now()
        FundRefreshRun.objects.filter(family=family, status__in=["queued", "running"], expires_at__lte=now).update(
            status="interrupted", message="任务已超过运行时限，请重新更新。", finished_at=now)
        active = FundRefreshRun.objects.select_for_update().filter(
            family=family, requested_by=user, status__in=["queued", "running"], expires_at__gt=now).first()
        requested = set(fund_ids)
        if active:
            extra = requested - set(active.fund_ids)
            if not extra:
                return active
            if active.status == "queued":
                active.fund_ids = sorted(set(active.fund_ids) | extra)
                active.save(update_fields=["fund_ids", "updated_at"])
                return active
            requested = extra
        job = FundRefreshRun.objects.create(family=family, requested_by=user,
            is_scheduled=user is None,
            fund_ids=sorted(requested), expires_at=now + timedelta(minutes=15),
            message="等待更新基金数据")
        if launch:
            transaction.on_commit(lambda: launch_job(job.pk))
        return job


def run_job(job_id):
    now = timezone.now()
    if not FundRefreshRun.objects.filter(pk=job_id, status="queued", expires_at__gt=now).update(
        status="running", started_at=now, message="正在更新基金净值及资料"):
        FundRefreshRun.objects.filter(pk=job_id, status="queued", expires_at__lte=now).update(
            status="interrupted", message="等待任务已过期，请重新更新。", finished_at=now)
        return
    job = FundRefreshRun.objects.select_related("requested_by").get(pk=job_id)
    try:
        plans = FundDcaPlan.objects.filter(family_id=job.family_id)
        user = job.requested_by
        if user is None and not job.is_scheduled:
            raise PermissionError()
        if user:
            member = FamilyMember.objects.filter(user=user, family_id=job.family_id, is_active=True).first()
            if not user.is_active or (member and member.role == FamilyMember.ROLE_VIEWER):
                raise PermissionError()
            if not member and not user.is_superuser:
                raise PermissionError()
            if member and member.role != FamilyMember.ROLE_ADMIN:
                plans = plans.filter(member=member)
        ids = set(plans.filter(fund_id__in=job.fund_ids).values_list("fund_id", flat=True))
        # Public target ETF data belongs to the same explicit fund information update.
        targets = FundReference.objects.filter(pk__in=ids, code="050025")
        if targets.exists():
            target, _ = FundReference.objects.get_or_create(code="513500")
            ids.add(target.pk)
        details = []
        for fund_id in sorted(ids):
            if not FundRefreshRun.objects.filter(pk=job_id, status="running", expires_at__gt=timezone.now()).exists():
                return
            details.append(refresh_fund(fund_id))
            FundRefreshRun.objects.filter(pk=job_id, status="running").update(details=details)
        failed = any(item["status"] != "saved" for item in details)
        status = ("partial" if any(item["status"] in ["saved", "partial"] for item in details) else "failed") if failed else "saved"
        if not details:
            status = "failed"
        FundRefreshRun.objects.filter(pk=job_id, status="running").update(status=status,
            details=details, finished_at=timezone.now(),
            message=f"已完成 {len(details)} 只基金的数据更新。" + ("部分数据未更新，请查看下方记录。" if failed else ""))
    except PermissionError:
        FundRefreshRun.objects.filter(pk=job_id, status="running").update(
            status="failed", message="申请人的维护权限已失效。", finished_at=timezone.now())
    except (Exception, JobDeadline):
        FundRefreshRun.objects.filter(pk=job_id, status="running").update(
            status="failed", message="基金更新异常，已保留此前成功数据。", finished_at=timezone.now())
        raise
