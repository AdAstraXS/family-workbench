"""Explicit opt-in AI explanations for frozen screening rows."""

from datetime import timedelta
from decimal import Decimal
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys

from django.db import transaction
from django.utils import timezone

from ai_analysis.models import AiAnalysisRequest, AiAnalysisResult
from family_core.models import Family, FamilyMember

from .advice_jobs import AdviceError, MODULE, cost_for, provider_configuration
from .models import WheelAnalysisJob
from .screen_advice import PROMPT, SCHEMA, build_packets, validate_result


MAX_TOTAL_ESTIMATED_USD = Decimal("0.02")
DAILY_REQUEST_LIMIT = 10
DEADLINE_SECONDS = 120


def screen_provider_configuration(provider=None):
    selected, config = provider_configuration(provider)
    config = {key: value for key, value in config.items() if key != "fingerprint"}
    config["prompt_hash"] = sha256(PROMPT.encode()).hexdigest()
    config["data_scope"] = "wheel_public_screen_v1"
    config["fingerprint"] = sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
    return selected, config


def estimated_cost(packet, config):
    content = PROMPT + json.dumps(packet, ensure_ascii=False)
    if len(content) > config["max_input_characters"]:
        raise AdviceError("本次合约过多，单批 AI 输入超过配置上限；规则分析已保存。")
    amount = cost_for(len(content.encode("utf-8")) + 1024, config["max_output_tokens"], config)
    if amount > Decimal(config["max_cost"]):
        raise AdviceError("单批 AI 费用预估超过配置上限；规则分析已保存。")
    return amount


def _launch(pk):
    platform = {"start_new_session": True} if os.name != "nt" else {
        "creationflags": subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
    }
    try:
        subprocess.Popen([sys.executable, "manage.py", "run_wheel_screen_advice", str(pk)],
            cwd=Path(__file__).resolve().parent.parent, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True, **platform)
    except OSError:
        AiAnalysisRequest.objects.filter(pk=pk, status="pending").update(
            status="failed", error_message="AI 进程未启动，规则建议仍可查看；未自动重试。")


def _mark_unavailable(job_id, message):
    with transaction.atomic():
        job = WheelAnalysisJob.objects.select_for_update().get(pk=job_id)
        if job.status == "saved" and "AI 建议未生成：" not in job.message:
            job.message += " AI 建议未生成：" + message
            job.save(update_fields=["message", "updated_at"])


def enqueue_for_screening(job_id):
    """Called once after frozen screening is committed; never from a GET."""
    try:
        with transaction.atomic():
            job = WheelAnalysisJob.objects.select_related("requested_by").get(pk=job_id)
            if job.status != "saved" or not job.selection.get("ai_enabled"):
                return
            Family.objects.select_for_update().get(pk=job.family_id)
            existing = AiAnalysisRequest.objects.filter(
                family=job.family, module=MODULE, analysis_type=SCHEMA, scope__job_id=str(job.pk))
            if existing.exists():
                return
            member = FamilyMember.objects.filter(
                family=job.family, user=job.requested_by, is_active=True).first()
            if not member or not job.requested_by.is_active or not job.requested_by.is_superuser:
                raise AdviceError("申请人的管理员权限已失效。")
            packets = build_packets(job)
            if not packets:
                raise AdviceError("没有符合本次范围的可比较合约。")
            provider, config = screen_provider_configuration()
            estimates = [estimated_cost(packet, config) for packet in packets]
            if sum(estimates, Decimal(0)) > MAX_TOTAL_ESTIMATED_USD:
                raise AdviceError("整次 AI 费用预估超过 $0.02 上限。")
            used = AiAnalysisRequest.objects.filter(
                family=job.family, module=MODULE, created_at__date=timezone.localdate()).count()
            if used + len(packets) > DAILY_REQUEST_LIMIT:
                raise AdviceError("今天剩余 AI 请求次数不足以覆盖全部显示合约。")
            pending = AiAnalysisRequest.objects.filter(family=job.family, module=MODULE, status="pending")
            if pending.filter(created_at__gt=timezone.now() - timedelta(seconds=DEADLINE_SECONDS)).exists():
                raise AdviceError("家庭已有 AI 分析正在运行。")
            requests = []
            for number, (packet, estimate) in enumerate(zip(packets, estimates), start=1):
                requests.append(AiAnalysisRequest.objects.create(
                    family=job.family, member=member, provider=provider, module=MODULE,
                    analysis_type=SCHEMA, prompt=PROMPT, sanitized_input=packet,
                    scope={"job_id": str(job.pk), "batch": number, "input_hash": packet["input_hash"],
                           "config_hash": config["fingerprint"], "config": config,
                           "estimated_max_cost": str(estimate), "phase": "queued",
                           "requested_user_id": job.requested_by_id,
                           "consent": "screen_checkbox_public_market_total_usd_0.02_v1"},
                ))
            transaction.on_commit(lambda: [_launch(item.pk) for item in requests])
    except AdviceError as exc:
        _mark_unavailable(job_id, str(exc))
    except Exception:
        _mark_unavailable(job_id, "AI 任务准备失败；规则分析已保存。")


def run_screen_advice(pk):
    with transaction.atomic():
        request = AiAnalysisRequest.objects.select_for_update().get(
            pk=pk, module=MODULE, analysis_type=SCHEMA)
        if request.status != "pending" or request.scope.get("phase") != "queued":
            return
        request.scope = {**request.scope, "phase": "running"}
        request.save(update_fields=["scope", "updated_at"])
    try:
        if request.created_at + timedelta(seconds=DEADLINE_SECONDS) <= timezone.now():
            raise AdviceError("AI 任务已超时，未调用。")
        if not FamilyMember.objects.filter(
                pk=request.member_id, family=request.family, is_active=True,
                user_id=request.scope["requested_user_id"], user__is_active=True,
                user__is_superuser=True).exists():
            raise AdviceError("申请人的权限已失效，未调用 AI。")
        job = WheelAnalysisJob.objects.get(pk=request.scope["job_id"], family=request.family, status="saved")
        if not job.selection.get("ai_enabled"):
            raise AdviceError("本次分析未开启 AI。")
        _, config = screen_provider_configuration(request.provider)
        if config["fingerprint"] != request.scope["config_hash"]:
            raise AdviceError("模型或费用配置已变化，未调用 AI。")
        packets = build_packets(job)
        batch = request.scope["batch"] - 1
        if batch >= len(packets) or packets[batch]["input_hash"] != request.scope["input_hash"]:
            raise AdviceError("冻结结果已变化，未调用 AI。")
        completed = subprocess.run(
            [sys.executable, "-m", "option_wheel.advice_transport", str(pk)],
            capture_output=True, text=True, encoding="utf-8", timeout=75, check=False,
        )
        if completed.returncode:
            raise AdviceError("DeepSeek 请求未成功或返回内容不合规；可能已产生费用，不自动重试。")
        reply = json.loads(completed.stdout)
        result = validate_result(reply["result"], request.sanitized_input)
        usage = reply.get("usage", {})
        counts = [usage.get("prompt_tokens"), usage.get("completion_tokens")]
        valid_usage = all(isinstance(value, int) and not isinstance(value, bool)
                          and 0 <= value <= 1000000 for value in counts)
        with transaction.atomic():
            request = AiAnalysisRequest.objects.select_for_update().get(pk=pk)
            if request.status != "pending" or request.created_at + timedelta(seconds=DEADLINE_SECONDS) <= timezone.now():
                raise AdviceError("AI 任务已过期，迟到结果未采纳。")
            if not FamilyMember.objects.filter(
                    pk=request.member_id, family=request.family, is_active=True,
                    user_id=request.scope["requested_user_id"], user__is_active=True,
                    user__is_superuser=True).exists():
                raise AdviceError("申请人权限已失效，结果未采纳。")
            _, current_config = screen_provider_configuration(request.provider)
            if current_config["fingerprint"] != request.scope["config_hash"]:
                raise AdviceError("模型配置已变化，结果未采纳。")
            AiAnalysisResult.objects.create(
                request=request, result_text="本批冻结合约的逐项 AI 建议", result_json=result,
                tokens_used=sum(counts) if valid_usage else None,
                cost_estimate=cost_for(*counts, config) if valid_usage else None,
            )
            request.status = "success"
            request.save(update_fields=["status", "updated_at"])
    except Exception as exc:
        message = str(exc) if isinstance(exc, AdviceError) else "AI 校验失败或中断；可能已产生费用，不自动重试。"
        AiAnalysisRequest.objects.filter(pk=pk, module=MODULE, analysis_type=SCHEMA, status="pending").update(
            status="failed", error_message=message)
