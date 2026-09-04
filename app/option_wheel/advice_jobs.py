"""On-demand DeepSeek explanation with private scope kept outside model input."""
from datetime import timedelta
from decimal import Decimal, InvalidOperation, ROUND_UP
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from django.db import transaction
from django.utils import timezone

from ai_analysis.models import AiAnalysisRequest, AiAnalysisResult, AiProvider
from family_core.models import Family, FamilyMember
from .advice import PROMPT, SCHEMA, validate_advice_result

MODULE = "option_wheel"
DEADLINE_SECONDS = 120
DAILY_REQUEST_LIMIT = 10


class AdviceError(ValueError):
    pass


def provider_configuration(provider=None):
    providers = AiProvider.objects.filter(is_active=True, provider_type="openai_compatible",
        model_name="deepseek-v4-flash", base_url__in=("https://api.deepseek.com", "https://api.deepseek.com/", "https://api.deepseek.com/v1"))
    if provider is None:
        rows = list(providers[:2])
        if len(rows) != 1:
            raise AdviceError("需要唯一的已启用 DeepSeek V4 Flash 配置；请在 AI 服务商中核对。")
        provider = rows[0]
    elif not providers.filter(pk=provider.pk).exists():
        raise AdviceError("原 DeepSeek 配置已停用或改变，请重新核对。")
    extra = provider.extra_data or {}
    env_name = extra.get("api_key_env_var", "")
    if (not isinstance(env_name, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{2,99}", env_name)
            or not os.getenv(env_name) or {"api_key", "apikey", "secret_key", "token", "access_token"}.intersection(extra)):
        raise AdviceError("DeepSeek 环境密钥未就绪；密钥不得保存在数据库中。")
    try:
        prices = [Decimal(str(extra[k])) for k in ("intelligence_input_usd_per_million",
            "intelligence_output_usd_per_million", "intelligence_max_estimated_usd")]
        if any(not p.is_finite() or p <= 0 for p in prices):
            raise ValueError
        output_limit = int(extra["intelligence_max_output_tokens"])
        input_limit = int(extra["intelligence_max_input_characters"])
        if not 256 <= output_limit <= 4096 or not 1000 <= input_limit <= 20000:
            raise ValueError
    except (KeyError, TypeError, ValueError, InvalidOperation):
        raise AdviceError("DeepSeek 已有费用或长度限制不完整，尚不能发起调用。") from None
    config = {"provider_id": provider.pk, "model": provider.model_name,
        "api_key_env_var": env_name, "input_price": str(prices[0]), "output_price": str(prices[1]),
        "max_cost": str(min(prices[2], Decimal("0.01"))),
        "max_output_tokens": output_limit, "max_input_characters": input_limit,
        "data_scope": "wheel_public_market_v1", "thinking": "disabled",
        "pricing_verified_on": "2026-09-04",
        "prompt_hash": sha256(PROMPT.encode()).hexdigest()}
    config["fingerprint"] = sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
    return provider, config


def cost_for(input_tokens, output_tokens, config):
    return ((Decimal(input_tokens) * Decimal(config["input_price"]) +
        Decimal(output_tokens) * Decimal(config["output_price"])) / Decimal(1000000)).quantize(
            Decimal("0.000001"), rounding=ROUND_UP)


def check_request_cost(packet, config):
    text = PROMPT + json.dumps(packet, ensure_ascii=False)
    if len(text) > config["max_input_characters"]:
        raise AdviceError("公开输入超过本次允许长度，未调用 AI。")
    # Byte count is conservative for the configured tokenizer, with protocol overhead.
    estimated = cost_for(len(text.encode("utf-8")) + 1024, config["max_output_tokens"], config)
    if estimated > Decimal(config["max_cost"]):
        raise AdviceError("请求费用预估超过单次上限，未调用 AI。")
    return str(estimated)


def launch_advice(pk):
    platform = {"start_new_session": True} if os.name != "nt" else {
        "creationflags": subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS}
    try:
        subprocess.Popen([sys.executable, "manage.py", "run_wheel_advice", str(pk)],
            cwd=Path(__file__).resolve().parent.parent, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True, **platform)
    except OSError:
        AiAnalysisRequest.objects.filter(pk=pk, module=MODULE, status="pending").update(
            status="failed", error_message="AI 进程未启动，未自动重试。")


def enqueue_advice(*, decision, user, packet, provider, config, nonce):
    estimate = check_request_cost(packet, config)
    member = FamilyMember.objects.filter(family=decision.family, user=user, is_active=True).first()
    if not user.is_active or not user.is_superuser or member is None:
        raise AdviceError("仅有本家庭成员身份的管理员可以发起 AI 调用。")
    with transaction.atomic():
        Family.objects.select_for_update().get(pk=decision.family_id)
        requests = AiAnalysisRequest.objects.filter(family=decision.family, module=MODULE, analysis_type=SCHEMA)
        duplicate = requests.filter(scope__nonce=nonce).first()
        if duplicate:
            return duplicate
        # Exact same evidence + model + prompt reuses a saved result (no paid refresh).
        saved = requests.filter(status="success", scope__decision_id=decision.pk,
            scope__input_hash=packet["input_hash"], scope__config_hash=config["fingerprint"]).first()
        if saved:
            return saved
        active = requests.filter(status="pending").order_by("created_at").first()
        if active and active.created_at + timedelta(seconds=DEADLINE_SECONDS) > timezone.now():
            raise AdviceError("家庭已有 AI 分析正在运行，请先查看那份分析；不会重复调用。")
        requests.filter(status="pending", created_at__lte=timezone.now()-timedelta(seconds=DEADLINE_SECONDS)).update(
            status="failed", error_message="任务超时或中断，结果未采纳；可能已产生费用，不自动重试。")
        if requests.filter(created_at__date=timezone.localdate()).count() >= DAILY_REQUEST_LIMIT:
            raise AdviceError("本家庭今天已达到 10 次请求上限；失败请求也计入，避免重复费用。")
        request = AiAnalysisRequest.objects.create(family=decision.family, member=member, provider=provider,
            module=MODULE, analysis_type=SCHEMA, prompt=PROMPT, sanitized_input=packet,
            scope={"decision_id": decision.pk, "nonce": nonce, "input_hash": packet["input_hash"],
                "config_hash": config["fingerprint"], "config": config, "estimated_max_cost": estimate,
                "phase": "queued", "requested_user_id": user.pk, "consent": "public_market_only_max_usd_0.01_v1"})
        transaction.on_commit(lambda: launch_advice(request.pk))
    return request


def run_advice(pk):
    with transaction.atomic():
        request = AiAnalysisRequest.objects.select_for_update().get(pk=pk, module=MODULE, analysis_type=SCHEMA)
        if request.status != "pending" or request.scope.get("phase") != "queued":
            return
        request.scope = {**request.scope, "phase": "running"}
        request.save(update_fields=["scope", "updated_at"])
    try:
        if request.created_at + timedelta(seconds=DEADLINE_SECONDS) <= timezone.now():
            raise AdviceError("任务已超时，未调用 AI。")
        if not FamilyMember.objects.filter(pk=request.member_id, family=request.family, is_active=True,
                user_id=request.scope["requested_user_id"], user__is_active=True, user__is_superuser=True).exists():
            raise AdviceError("申请人的成员或管理员权限已失效，未调用 AI。")
        _, config = provider_configuration(request.provider)
        if config["fingerprint"] != request.scope["config_hash"]:
            raise AdviceError("模型或费用配置已变化，未调用 AI，请重新核对。")
        completed = subprocess.run([sys.executable, "-m", "option_wheel.advice_transport", str(pk)],
            capture_output=True, text=True, encoding="utf-8", timeout=75, check=False)
        if completed.returncode:
            raise AdviceError("DeepSeek 请求未成功或返回内容不合规；可能已产生费用，不自动重试。")
        reply = json.loads(completed.stdout)
        result = validate_advice_result(reply["result"], request.sanitized_input)
        usage = reply.get("usage", {})
        counts = [usage.get("prompt_tokens"), usage.get("completion_tokens")]
        valid_usage = all(isinstance(n, int) and not isinstance(n, bool) and 0 <= n <= 1000000 for n in counts)
        with transaction.atomic():
            request = AiAnalysisRequest.objects.select_for_update().get(pk=pk)
            if request.status != "pending" or request.created_at + timedelta(seconds=DEADLINE_SECONDS) <= timezone.now():
                raise AdviceError("任务已超时或终止，迟到结果未采纳。")
            if not FamilyMember.objects.filter(pk=request.member_id, family=request.family, is_active=True,
                    user_id=request.scope["requested_user_id"], user__is_active=True, user__is_superuser=True).exists():
                raise AdviceError("申请人的权限已失效，结果未采纳。")
            _, current_config = provider_configuration(request.provider)
            if current_config["fingerprint"] != request.scope["config_hash"]:
                raise AdviceError("模型配置已变化，结果未采纳。")
            AiAnalysisResult.objects.create(request=request, result_text=result["summary"], result_json=result,
                tokens_used=sum(counts) if valid_usage else None,
                cost_estimate=cost_for(*counts, config) if valid_usage else None)
            request.status = "success"
            request.save(update_fields=["status", "updated_at"])
    except Exception as exc:
        message = str(exc) if isinstance(exc, AdviceError) else "AI 响应校验失败或运行中断；可能已产生费用，不自动重试。"
        AiAnalysisRequest.objects.filter(pk=pk, module=MODULE, status="pending").update(status="failed", error_message=message)


def request_state(request):
    if request is None:
        return "none"
    if request.status == "pending" and request.created_at + timedelta(seconds=DEADLINE_SECONDS) <= timezone.now():
        return "interrupted"
    return request.status
