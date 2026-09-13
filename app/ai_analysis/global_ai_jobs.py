"""DeepSeek execution loop for the private global AI workbench."""

from decimal import Decimal, InvalidOperation, ROUND_UP
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .global_ai_runtime import TOOL_SCHEMAS, GlobalAiRuntimeError, dispatch_read_tool
from .global_ai_services import (
    GlobalAiServiceError,
    acknowledge_global_ai_cancellation,
    claim_global_ai_request,
    complete_global_ai_request,
    fail_global_ai_request,
    mark_global_ai_request_unknown,
    prepare_conversation_context,
)
from .models import AiAnalysisRequest, AiProvider


SYSTEM_PROMPT = """你是家庭工作台的私人 AI 助手。只依据对话和服务器工具返回的资料回答。
需要事实时调用工具；资料不足就明确说明。整体资产只查账本正式资产快照，具体投资账户只查投资模块，
收入、支出和预算只查账本收支预算工具，不把资产变动当作收入或支出。不要自动核对、合并或解释账本与
投资模块之间的差额。金额保持工具返回的精度，不承诺收益，不执行写入或交易。
用简洁中文回答，并清楚说明结论依据来自哪个模块。"""
ENDPOINT = "https://api.deepseek.com/chat/completions"
SECRET_FIELDS = {"api_key", "apikey", "secret_key", "token", "access_token"}
MAX_RESPONSE_BYTES = 262144


class GlobalAiJobError(ValueError):
    pass


class GlobalAiNetworkUncertain(RuntimeError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise GlobalAiNetworkUncertain("AI 服务发生了未允许的跳转。")


def provider_configuration(provider=None):
    rows = AiProvider.objects.filter(
        is_active=True,
        provider_type="openai_compatible",
        model_name="deepseek-v4-pro",
        base_url__in=("https://api.deepseek.com", "https://api.deepseek.com/", "https://api.deepseek.com/v1"),
        extra_data__global_ai_enabled=True,
    )
    if provider is None:
        providers = list(rows[:2])
        if len(providers) != 1:
            raise GlobalAiJobError("需要唯一的已启用 DeepSeek V4-Pro 全局 AI 配置。")
        provider = providers[0]
    elif not rows.filter(pk=provider.pk).exists():
        raise GlobalAiJobError("全局 AI 模型配置已停用或改变。")
    extra = provider.extra_data or {}
    env_name = extra.get("api_key_env_var", "")
    if (
        not isinstance(env_name, str)
        or not re.fullmatch(r"[A-Z][A-Z0-9_]{2,99}", env_name)
        or not os.getenv(env_name)
        or SECRET_FIELDS.intersection(extra)
    ):
        raise GlobalAiJobError("DeepSeek 环境密钥未就绪；密钥不得保存在数据库中。")
    try:
        input_price = Decimal(str(extra["global_ai_input_usd_per_million"]))
        output_price = Decimal(str(extra["global_ai_output_usd_per_million"]))
        timeout = int(extra.get("global_ai_timeout_seconds", 45))
        loop_timeout = int(extra.get("global_ai_loop_timeout_seconds", 180))
        if (
            any(not item.is_finite() or item <= 0 for item in (input_price, output_price))
            or not 10 <= timeout <= 60
            or not 30 <= loop_timeout <= 600
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError, InvalidOperation):
        raise GlobalAiJobError("全局 AI 的费用或超时配置不完整。") from None
    config = {
        "provider_id": provider.pk,
        "model": provider.model_name,
        "api_key_env_var": env_name,
        "input_price": str(input_price),
        "output_price": str(output_price),
        "timeout_seconds": timeout,
        "loop_timeout_seconds": loop_timeout,
        "prompt_hash": sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest(),
        "data_scope": "conversation_tools_v1",
    }
    config["fingerprint"] = sha256(
        json.dumps(config, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return provider, config


def _cost(input_tokens, output_tokens, config):
    return ((
        Decimal(input_tokens) * Decimal(config["input_price"])
        + Decimal(output_tokens) * Decimal(config["output_price"])
    ) / Decimal(1000000)).quantize(Decimal("0.000001"), rounding=ROUND_UP)


def launch_global_ai_request(request_id):
    platform = {"start_new_session": True} if os.name != "nt" else {
        "creationflags": subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
    }
    try:
        subprocess.Popen(
            [sys.executable, "manage.py", "run_global_ai_request", str(request_id)],
            cwd=Path(__file__).resolve().parent.parent,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            **platform,
        )
    except OSError:
        AiAnalysisRequest.objects.filter(pk=request_id, status="pending", module="global_ai").update(
            status="failed", error_message="AI 后台任务未启动，请稍后重新提问。"
        )


def launch_global_ai_knowledge_evaluation(member_id):
    platform = {"start_new_session": True} if os.name != "nt" else {
        "creationflags": subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
    }
    try:
        subprocess.Popen(
            [
                sys.executable,
                "manage.py",
                "evaluate_global_ai_knowledge",
                "--member-id",
                str(member_id),
                "--confirm-cloud-run",
            ],
            cwd=Path(__file__).resolve().parent.parent,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            **platform,
        )
    except OSError as exc:
        raise GlobalAiJobError("知识验收后台任务未能启动。") from exc


def _post_json(payload, config):
    request = Request(
        ENDPOINT,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + os.environ[config["api_key_env_var"]],
        },
        method="POST",
    )
    try:
        with build_opener(NoRedirect()).open(request, timeout=config["timeout_seconds"]) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        if 400 <= exc.code < 500:
            raise GlobalAiJobError("AI 服务拒绝了本次请求，未自动重试。") from exc
        raise GlobalAiNetworkUncertain("AI 服务连接中断，结果状态未知。") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise GlobalAiNetworkUncertain("AI 服务连接中断，结果状态未知。") from exc
    if len(raw) > MAX_RESPONSE_BYTES:
        raise GlobalAiJobError("AI 返回内容超过安全长度。")
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GlobalAiJobError("AI 返回内容无法读取。") from exc


def _cancel_requested(request_id, token):
    status = AiAnalysisRequest.objects.filter(pk=request_id, execution_token=token).values_list(
        "status", flat=True
    ).first()
    return status == AiAnalysisRequest.STATUS_CANCEL_REQUESTED


def execute_model_loop(request, config, *, post_json=_post_json):
    context = prepare_conversation_context(
        request.member, conversation_id=request.conversation_id, provider=request.provider
    )
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for item in context["messages"]:
        role = item["role"] if item["role"] in {"user", "assistant"} else "assistant"
        messages.append({"role": role, "content": item["content"]})
    evidence_refs, data_types = [], set()
    prompt_tokens = completion_tokens = 0
    http_requests = 0
    started_at = time.monotonic()
    while True:
        if _cancel_requested(request.pk, request.execution_token):
            raise InterruptedError
        if time.monotonic() - started_at >= config["loop_timeout_seconds"]:
            raise GlobalAiJobError("AI 处理时间超过上限，请稍后新建对话重试。")
        http_requests += 1
        reply = post_json({
            "model": config["model"],
            "thinking": {"type": "disabled"},
            "messages": messages,
            "tools": TOOL_SCHEMAS,
            "tool_choice": "auto",
        }, config)
        try:
            choice = reply["choices"][0]
            message = choice["message"]
            usage = reply.get("usage") or {}
            for key, target in (("prompt_tokens", "prompt"), ("completion_tokens", "completion")):
                value = usage.get(key, 0)
                if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                    raise GlobalAiJobError("AI 用量数据不可用。")
                if target == "prompt": prompt_tokens += value
                else: completion_tokens += value
        except (KeyError, IndexError, TypeError) as exc:
            raise GlobalAiJobError("AI 返回结构不可用。") from exc
        tool_calls = message.get("tool_calls") or []
        if tool_calls:
            messages.append(message)
            for call in tool_calls:
                try:
                    arguments_text = call["function"].get("arguments", "{}")
                    if len(arguments_text) > 4000:
                        raise GlobalAiJobError("AI 工具参数过长。")
                    arguments = json.loads(arguments_text)
                    tool_result = dispatch_read_tool(
                        request.member,
                        conversation=request.conversation,
                        provider=request.provider,
                        name=call["function"]["name"],
                        arguments=arguments,
                    )
                except (KeyError, TypeError, json.JSONDecodeError, GlobalAiRuntimeError) as exc:
                    raise GlobalAiJobError(str(exc) or "AI 工具请求不可用。") from exc
                tool_evidence_refs = tool_result["evidence_refs"]
                if tool_evidence_refs:
                    data_types.add(tool_result["data_type"])
                    evidence_refs.extend(tool_evidence_refs)
                messages.append({
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": json.dumps(tool_result["result"], ensure_ascii=False, separators=(",", ":")),
                })
            continue
        content = message.get("content")
        if choice.get("finish_reason") != "stop" or not isinstance(content, str) or not content.strip():
            raise GlobalAiJobError("AI 回答未完整结束。")
        unique_refs = []
        seen_refs = set()
        for reference in evidence_refs:
            key = json.dumps(reference, sort_keys=True, separators=(",", ":"))
            if key not in seen_refs:
                seen_refs.add(key)
                unique_refs.append(reference)
        return {
            "content": content.strip(),
            "data_types": sorted(data_types),
            "evidence_refs": unique_refs,
            "tokens_used": prompt_tokens + completion_tokens,
            "cost": _cost(prompt_tokens, completion_tokens, config),
            "http_requests": http_requests,
        }
    raise GlobalAiJobError("AI 在限定步骤内没有形成最终回答。")


def run_global_ai_request(request_id):
    token = claim_global_ai_request(request_id=request_id)
    request = AiAnalysisRequest.objects.select_related("member", "member__family", "conversation", "provider").get(
        pk=request_id
    )
    try:
        _provider, config = provider_configuration(request.provider)
        if request.scope.get("config_fingerprint") != config["fingerprint"]:
            raise GlobalAiJobError("模型配置已变化，请重新提问。")
        result = execute_model_loop(request, config)
        complete_global_ai_request(
            request_id=request.pk,
            execution_token=token,
            result_text=result["content"],
            result_json={"http_requests": result["http_requests"]},
            tokens_used=result["tokens_used"],
            cost_estimate=result["cost"],
            message_data_types=result["data_types"],
            evidence_refs=result["evidence_refs"],
        )
    except InterruptedError:
        acknowledge_global_ai_cancellation(request_id=request.pk, execution_token=token)
    except GlobalAiNetworkUncertain:
        if _cancel_requested(request.pk, token):
            acknowledge_global_ai_cancellation(request_id=request.pk, execution_token=token)
        else:
            mark_global_ai_request_unknown(request_id=request.pk, execution_token=token)
    except (GlobalAiJobError, GlobalAiServiceError) as exc:
        if _cancel_requested(request.pk, token):
            acknowledge_global_ai_cancellation(request_id=request.pk, execution_token=token)
        else:
            fail_global_ai_request(request_id=request.pk, execution_token=token, error_message=str(exc))
    except Exception:
        if _cancel_requested(request.pk, token):
            acknowledge_global_ai_cancellation(request_id=request.pk, execution_token=token)
        else:
            fail_global_ai_request(
                request_id=request.pk,
                execution_token=token,
                error_message="AI 响应校验失败或运行中断；不会自动重试。",
            )
