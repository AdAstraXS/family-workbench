"""Small durable, bounded on-demand jobs; no scheduler or broker required."""
from datetime import date, timedelta
from zoneinfo import ZoneInfo
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
from .analysis_service import WheelAnalysisError, covered_position, persist_probe_symbol
from .models import WheelAnalysisJob, WheelBrokerAccountSnapshot, WheelPolicy
from .probe_diagnostics import probe_failure_summary

ACTIVE = ("queued", "running")
JOB_SECONDS = 720
PROBE_SECONDS = 180
MAX_SYMBOLS = 20
PROBE_BATCH_SIZE = 3
INTERRUPTED = "运行超时或中断，未取得完成确认。不会自动重试；行情订阅清理状态需核对。"


def validate_selection(family, selection):
    from .views import PARTICIPATING_ACCOUNTS, _snapshot_is_ready
    from .account_capacity import capacity_snapshot_stale_reasons
    if selection.get("mode") in ("screening_v2", "screening_close_v2"):
        from .models import WheelWatchItem
        from .screening import number
        symbols = selection.get("symbols", [])
        if not symbols or len(symbols) > MAX_SYMBOLS or len(set(symbols)) != len(symbols):
            raise WheelAnalysisError("请选择 1–20 个自选标的。")
        configured = set(WheelWatchItem.objects.filter(family=family, symbol__in=symbols).values_list("symbol", flat=True))
        if configured != set(symbols):
            raise WheelAnalysisError("所选股票不在当前家庭的期权自选列表。")
        try:
            expiry = date.fromisoformat(selection["target_expiration"])
            premium_min, premium_max = number(selection["premium_min"]), number(selection["premium_max"])
        except (KeyError, TypeError, ValueError):
            raise WheelAnalysisError("到期日或权利金范围无效。") from None
        ny_today = timezone.now().astimezone(ZoneInfo("America/New_York")).date()
        if expiry.weekday() != 4 or not 0 < (expiry - ny_today).days <= 35:
            raise WheelAnalysisError("请选择未来 35 天内的一个周五到期日。")
        if premium_min is None or premium_max is None or premium_min < 0 or premium_max < premium_min or premium_max > 100000:
            raise WheelAnalysisError("权利金范围无效。")
        return []
    ids, symbols = selection.get("account_ids", []), selection.get("symbols", [])
    target = selection.get("target_expiration")
    if target is not None:
        try:
            expiry = date.fromisoformat(target)
        except (TypeError, ValueError):
            raise WheelAnalysisError("目标到期日无效。") from None
        ny_today = timezone.now().astimezone(ZoneInfo("America/New_York")).date()
        if expiry.weekday() != 4 or not 0 < (expiry - ny_today).days <= 35:
            raise WheelAnalysisError("目标周五已经过去或超出可选范围，请重新提交。")
    if not ids or not symbols or len(ids) > 2 or len(symbols) > 9:
        raise WheelAnalysisError("每次请选择 1–2 个账户和 1–9 个已配置标的。")
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
            raise WheelAnalysisError("账户容量已变化、过期或未就绪，请重新预演并保存。")
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


def _fetch_probe_batch(symbols, covered_call_symbols, target_expiration=None, screening=False):
    covered_call_symbols = sorted(set(symbols) & set(covered_call_symbols))
    command = [sys.executable, "-m", "option_wheel.live_probe"]
    if screening:
        command.append("--screen")
    if covered_call_symbols:
        command.append("--calls-for=" + ",".join(covered_call_symbols))
    if target_expiration:
        command.append("--expiration=" + target_expiration)
    command.extend("US." + symbol for symbol in symbols)
    try:
        completed = subprocess.run(
            command,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=PROBE_SECONDS, check=False,
        )
        frames = [line[len("WHEEL_LIVE:"):] for line in completed.stdout.splitlines() if line.startswith("WHEEL_LIVE:")]
        if completed.returncode or len(frames) != 1:
            raise ValueError("invalid frame")
        result = json.loads(frames[0])
        if not isinstance(result, dict):
            raise ValueError("invalid payload")
        if result.get("status") not in (("success", "partial") if screening else ("success",)):
            raise WheelAnalysisError("本次未保存分析。" + probe_failure_summary(result, symbols))
        if screening and result.get("subscription", {}).get("cleanup_status") != "restored":
            raise WheelAnalysisError("行情订阅未确认恢复，本次未保存分析。")
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


def fetch_probe(symbols, covered_call_symbols=None, target_expiration=None, screening=False):
    """Probe at most three symbols per process so Futu subscriptions stay bounded."""
    covered_call_symbols = set(covered_call_symbols or [])
    rows = []
    for offset in range(0, len(symbols), PROBE_BATCH_SIZE):
        batch = symbols[offset:offset + PROBE_BATCH_SIZE]
        rows.extend(_fetch_probe_batch(batch, covered_call_symbols, target_expiration, screening))
    return rows


def covered_call_symbols(family, accounts, symbols):
    """Return symbols with an unencumbered round lot in any selected account."""
    eligible = set()
    for account in accounts:
        snapshot = WheelBrokerAccountSnapshot.objects.filter(
            family=family, account=account,
        ).order_by("-source_as_of", "-pk").first()
        if snapshot is None:
            continue
        for symbol in symbols:
            available_shares, _ = covered_position(snapshot, symbol)
            if available_shares >= 100:
                eligible.add(symbol)
    return eligible


def run_job(job_id):
    now = timezone.now()
    if not WheelAnalysisJob.objects.filter(pk=job_id, status="queued", expires_at__gt=now).update(status="running", started_at=now):
        return
    try:
        job = WheelAnalysisJob.objects.select_related("family", "requested_by").get(pk=job_id)
        if not job.requested_by.is_active or not job.requested_by.is_superuser:
            raise WheelAnalysisError("申请人的管理员权限已失效，未保存分析。")
        accounts = validate_selection(job.family, job.selection)
        if job.selection.get("mode") == "screening_close_v2":
            from portfolio.futu_option_probe import ProbeLock
            from .close_data import CloseDataError
            from .models import WheelWatchItem
            from .screen_close import fetch
            from .screening import compare_close_rows, covered_stock

            holdings = covered_stock(job.family, job.selection["symbols"])

            lock = ProbeLock()
            if not lock.acquire():
                raise WheelAnalysisError("已有 Futu 行情查询正在运行，请稍后重新提交。")
            try:
                report = fetch(job.selection["symbols"], job.selection["target_expiration"], calls_for=set(holdings))
            except CloseDataError as exc:
                raise WheelAnalysisError(str(exc)) from exc
            finally:
                lock.release()
            watch_events = {item.symbol: item for item in WheelWatchItem.objects.filter(
                family=job.family, symbol__in=job.selection["symbols"],
            )}
            results = compare_close_rows(report, job.selection, watch_events, holdings)
            issues = [f'{item["symbol"]}：' + "、".join(item["issues"])
                      for item in report["symbols"] if item["issues"]]
            with transaction.atomic():
                job = WheelAnalysisJob.objects.select_for_update().get(pk=job_id)
                if job.status != "running" or job.expires_at <= timezone.now():
                    raise WheelAnalysisError("任务已中断或超过保存期限，未保存分析。")
                validate_selection(job.family, job.selection)
                job.status = "saved"
                job.screening_results = results
                job.message = (f'Futu {report["reference_date"]} 收盘参考：已列出 {len(results)} 张合约。'
                               + ("；" + "；".join(issues) if issues else ""))
                job.finished_at = timezone.now()
                job.save(update_fields=["status", "screening_results", "message", "finished_at", "updated_at"])
            return
        if job.selection.get("mode") == "screening_v2":
            from .screening import compare_probe_rows, covered_stock
            from .models import WheelWatchItem
            holdings = covered_stock(job.family, job.selection["symbols"])
            watch_events = {item.symbol: item for item in WheelWatchItem.objects.filter(
                family=job.family, symbol__in=job.selection["symbols"],
            )}
            rows = fetch_probe(
                job.selection["symbols"], covered_call_symbols=set(holdings),
                target_expiration=job.selection["target_expiration"], screening=True,
            )
            results = compare_probe_rows(rows, job.selection, holdings, watch_events)
            with transaction.atomic():
                job = WheelAnalysisJob.objects.select_for_update().get(pk=job_id)
                if job.status != "running" or job.expires_at <= timezone.now():
                    raise WheelAnalysisError("任务已中断或超过保存期限，未保存分析。")
                validate_selection(job.family, job.selection)
                if not job.requested_by.is_active or not job.requested_by.is_superuser:
                    raise WheelAnalysisError("申请人的管理员权限已失效，未保存分析。")
                job.status = "saved"
                job.screening_results = results
                job.message = f"已比较 {len(results)} 张合约；报价来自 Futu，临时订阅已恢复。"
                if not results:
                    job.message += " 所选到期日没有取得可用 Bid；可能是非交易时段、期权链为空或报价缺失。可选择上一交易日收盘参考分析。"
                job.finished_at = timezone.now()
                job.save(update_fields=["status", "screening_results", "message", "finished_at", "updated_at"])
            return
        call_symbols = covered_call_symbols(
            job.family, accounts, job.selection["symbols"]
        )
        probe_kwargs = {"covered_call_symbols": call_symbols}
        if job.selection.get("target_expiration"):
            probe_kwargs["target_expiration"] = job.selection["target_expiration"]
        rows = fetch_probe(job.selection["symbols"], **probe_kwargs)
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
    close_mode = job.selection.get("mode") == "screening_close_v2"
    if status in ACTIVE and job.expires_at <= timezone.now():
        status, message = "interrupted", (
            "运行超时或中断，未取得完成确认。不会自动重试。" if close_mode else INTERRUPTED
        )
    return {
        "kind": "option-wheel-job-v1", "id": str(job.pk), "status": status,
        "label": dict(WheelAnalysisJob._meta.get_field("status").choices).get(status, status),
        "message": "当前任务：" + " / ".join(job.selection.get("account_names", []) + job.selection.get("symbols", [])) + ("；目标到期日 " + job.selection["target_expiration"] if job.selection.get("target_expiration") else "") + "。" + (message or (("正在查询 Futu 历史收盘数据，请勿重复提交。" if close_mode else "正在查询行情及核对订阅清理，请勿重复提交。") if status == "running" else "任务已受理，等待分析进程启动。")),
        "selection": job.selection, "created_at": job.created_at.isoformat(),
        "status_url": reverse("option_wheel:job_status", args=[job.pk]),
        "detail_url": reverse("option_wheel:job_detail", args=[job.pk]),
        "results": [{"id": pk, "url": reverse("option_wheel:decision_detail", args=[pk])} for pk in job.decision_ids],
    }
