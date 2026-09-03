"""Small durable, bounded on-demand jobs; no scheduler or broker required."""
from datetime import timedelta
import json
import os
from pathlib import Path
import subprocess
import sys

from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from family_core.models import Family
from portfolio.models import InvestmentAccount
from .analysis_service import WheelAnalysisError, persist_probe_symbol
from .models import WheelAnalysisJob, WheelBrokerAccountSnapshot, WheelPolicy
from .probe_diagnostics import probe_failure_summary

ACTIVE = ("queued", "running")
JOB_SECONDS = 240
PROBE_SECONDS = 180
INTERRUPTED = "运行超时或中断，未取得完成确认。不会自动重试；行情订阅清理状态需核对。"


def validate_selection(family, selection):
    from .views import PARTICIPATING_ACCOUNTS, _snapshot_is_ready
    from .account_capacity import capacity_snapshot_stale_reasons
    ids, symbols = selection.get("account_ids", []), selection.get("symbols", [])
    if not ids or not symbols or len(ids) > 2 or len(symbols) > 3:
        raise WheelAnalysisError("每次请选择 1–2 个账户和 1–3 个已配置标的。")
    accounts = list(InvestmentAccount.objects.filter(
        pk__in=ids, bank_account__family=family, bank_account__is_active=True,
        bank_account__supports_investment=True,
        bank_account__account_name__in=PARTICIPATING_ACCOUNTS,
    ).select_related("bank_account"))
    if len(accounts) != len(ids):
        raise WheelAnalysisError("账户不属于当前家庭的车轮参与范围。")
    policies = list(WheelPolicy.objects.filter(
        family=family, account_id__in=ids, underlying__symbol__in=symbols, enabled=True,
    ).select_related("underlying"))
    if {(p.account_id, p.underlying.symbol.upper()) for p in policies} != {(a, s) for a in ids for s in symbols}:
        raise WheelAnalysisError("所选账户与标的尚未全部配置启用策略。")
    for account in accounts:
        snapshot = WheelBrokerAccountSnapshot.objects.filter(
            family=family, account=account,
        ).order_by("-source_as_of", "-pk").first()
        if not _snapshot_is_ready(snapshot, now=timezone.now(),
                max_age_minutes=min(p.account_snapshot_max_age_minutes for p in policies if p.account_id == account.pk),
                stale_reasons=capacity_snapshot_stale_reasons(snapshot) if snapshot else []):
            raise WheelAnalysisError("账户容量已变化、过期或未就绪，请重新预演并确认。")
    return accounts


def launch_job(job_id):
    """Independent process survives the short HTTP request, not a container restart."""
    kwargs = {"start_new_session": True} if os.name != "nt" else {
        "creationflags": subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
    }
    try:
        subprocess.Popen(
            [sys.executable, "manage.py", "run_wheel_analysis_job", str(job_id)],
            cwd=Path(__file__).resolve().parent.parent,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True, **kwargs,
        )
    except OSError:
        WheelAnalysisJob.objects.filter(pk=job_id, status="queued").update(
            status="failed", message="分析进程未能启动，未保存分析。", finished_at=timezone.now(),
        )


def enqueue(family, user, key, selection):
    with transaction.atomic():
        # Serialize starts, including different tokens from multiple tabs.
        Family.objects.select_for_update().get(pk=family.pk)
        existing = WheelAnalysisJob.objects.filter(pk=key, family=family).first()
        if existing:
            return existing
        active = WheelAnalysisJob.objects.select_for_update().filter(family=family, status__in=ACTIVE).first()
        if active and active.expires_at > timezone.now():
            return active
        if active:
            active.status, active.message, active.finished_at = "interrupted", INTERRUPTED, timezone.now()
            active.save(update_fields=["status", "message", "finished_at", "updated_at"])
        accounts = validate_selection(family, selection)
        job = WheelAnalysisJob.objects.create(
            id=key, family=family, requested_by=user,
            selection={**selection, "account_names": [a.account_name for a in accounts]},
            expires_at=timezone.now() + timedelta(seconds=JOB_SECONDS),
        )
        transaction.on_commit(lambda: launch_job(job.pk))
    return job


def fetch_probe(symbols):
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "option_wheel.live_probe", *["US." + s for s in symbols]],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=PROBE_SECONDS, check=False,
        )
        frames = [line[len("WHEEL_LIVE:"):] for line in completed.stdout.splitlines() if line.startswith("WHEEL_LIVE:")]
        if completed.returncode or len(frames) != 1:
            raise ValueError("invalid frame")
        result = json.loads(frames[0])
        if not isinstance(result, dict):
            raise ValueError("invalid payload")
        if result.get("status") != "success":
            raise WheelAnalysisError("本次未保存分析。" + probe_failure_summary(result, symbols))
        rows = result.get("symbols", [])
        if len(rows) != len(symbols) or {r.get("symbol") for r in rows} != {"US." + s for s in symbols}:
            raise ValueError("incomplete result")
        return rows
    except subprocess.TimeoutExpired:
        raise WheelAnalysisError("行情查询超过 180 秒，已结束查询进程，未保存分析；订阅清理状态需核对。") from None
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        if isinstance(exc, WheelAnalysisError):
            raise
        raise WheelAnalysisError("行情查询进程或响应异常，未保存分析；订阅清理状态需核对。") from None


def run_job(job_id):
    now = timezone.now()
    if not WheelAnalysisJob.objects.filter(pk=job_id, status="queued", expires_at__gt=now).update(status="running", started_at=now):
        return
    try:
        job = WheelAnalysisJob.objects.select_related("family", "requested_by").get(pk=job_id)
        if not job.requested_by.is_active or not job.requested_by.is_superuser:
            raise WheelAnalysisError("申请人的管理员权限已失效，未保存分析。")
        validate_selection(job.family, job.selection)
        rows = fetch_probe(job.selection["symbols"])
        with transaction.atomic():
            job = WheelAnalysisJob.objects.select_for_update().get(pk=job_id)
            if job.status != "running" or job.expires_at <= timezone.now():
                raise WheelAnalysisError("任务已中断或超过保存期限，未保存分析。")
            if not job.requested_by.is_active or not job.requested_by.is_superuser:
                raise WheelAnalysisError("申请人的管理员权限已失效，未保存分析。")
            accounts = validate_selection(job.family, job.selection)
            shared_quotes = {}
            decisions = [persist_probe_symbol(family=job.family, account=a, symbol_result=row, shared_quotes=shared_quotes)
                         for a in accounts for row in rows]
            if job.expires_at <= timezone.now():
                raise WheelAnalysisError("任务超过保存期限，已回滚本次分析。")
            job.status = "saved"
            job.message = f"已保存 {len(decisions)} 份只读分析；订阅恢复核对已通过，交易连接和下单闸门保持关闭。"
            job.decision_ids = [d.pk for d in decisions]
            job.finished_at = timezone.now()
            job.save(update_fields=["status", "message", "decision_ids", "finished_at", "updated_at"])
    except Exception as exc:
        message = str(exc) if isinstance(exc, WheelAnalysisError) else "分析处理异常，未保存本次分析；请检查服务状态。"
        WheelAnalysisJob.objects.filter(pk=job_id, status="running").update(
            status="failed", message=message, finished_at=timezone.now(),
        )


def job_payload(job):
    status = job.status
    message = job.message
    if status in ACTIVE and job.expires_at <= timezone.now():
        status, message = "interrupted", INTERRUPTED
    return {
        "kind": "option-wheel-job-v1", "id": str(job.pk), "status": status,
        "label": dict(WheelAnalysisJob._meta.get_field("status").choices).get(status, status),
        "message": "当前任务：" + " / ".join(job.selection.get("account_names", []) + job.selection.get("symbols", [])) + "。" + (message or ("正在查询行情及核对订阅清理，请勿重复提交。" if status == "running" else "任务已受理，等待分析进程启动。")),
        "selection": job.selection, "created_at": job.created_at.isoformat(),
        "status_url": reverse("option_wheel:job_status", args=[job.pk]),
        "detail_url": reverse("option_wheel:job_detail", args=[job.pk]),
        "results": [{"id": pk, "url": reverse("option_wheel:decision_detail", args=[pk])} for pk in job.decision_ids],
    }
