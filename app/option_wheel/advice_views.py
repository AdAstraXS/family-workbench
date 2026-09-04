import json
from uuid import uuid4

from django.contrib.auth.decorators import login_required
from django.core import signing
from django.http import HttpResponseBadRequest, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from .advice import build_advice_packet
from .advice_jobs import (AdviceError, MODULE, SCHEMA, provider_configuration, check_request_cost,
    enqueue_advice, request_state)
from ai_analysis.models import AiAnalysisRequest, AiAnalysisResult
from .models import WheelDecision
from .views import _request_family


@login_required
@require_GET
def preview(request, pk):
    decision = get_object_or_404(WheelDecision.objects.select_related(
        "underlying", "account__bank_account", "market_snapshot", "technical_snapshot", "event_snapshot",
    ), pk=pk, family=_request_family(request))
    data = build_advice_packet(decision, list(decision.candidates.select_related("option_quote")))
    requests = AiAnalysisRequest.objects.filter(family=decision.family, module=MODULE,
        analysis_type=SCHEMA, scope__decision_id=decision.pk).select_related("provider")
    if request.GET.get("request"):
        try:
            request_id = int(request.GET["request"])
        except ValueError:
            return HttpResponseBadRequest("请求编号无效。")
        analysis = get_object_or_404(requests, pk=request_id)
    else:
        analysis = requests.order_by("-created_at", "-pk").first()
    state = request_state(analysis)
    if request.headers.get("Accept") == "application/json":
        response = JsonResponse({"status": state})
        response["Cache-Control"] = "no-store"
        return response
    provider, config, configuration_error, estimated = None, None, "", ""
    try:
        provider, config = provider_configuration()
        estimated = check_request_cost(data["packet"], config)
    except AdviceError as exc:
        configuration_error = str(exc)
    token = signing.dumps({"family": decision.family_id, "decision": decision.pk,
        "input_hash": data["packet"]["input_hash"], "config_hash": config["fingerprint"] if config else "",
        "nonce": str(uuid4())}, salt="wheel-ai-consent-v1")
    result = AiAnalysisResult.objects.filter(request=analysis).first() if analysis and state == "success" else None
    comparisons = []
    if result:
        # Render contract identity and numbers from server input, never from model prose.
        public = {c["candidate_id"]: c for c in analysis.sanitized_input["candidates"]}
        comparisons = [{**row, "candidate": public[row["candidate_id"]]}
            for row in result.result_json.get("comparisons", []) if row.get("candidate_id") in public]
    response = render(request, "option_wheel/advice_preview.html", {
        "decision": decision, **data,
        "public_input": json.dumps(data["packet"], ensure_ascii=False, indent=2),
        "provider": provider, "ai_config": config, "configuration_error": configuration_error,
        "estimated_cost": estimated, "consent_token": token, "analysis": analysis,
        "ai_state": state, "ai_result": result, "comparisons": comparisons,
    })
    response["Cache-Control"] = "no-store"
    return response


@login_required
@require_POST
def generate(request, pk):
    from django.core.exceptions import PermissionDenied
    if not request.user.is_superuser:
        raise PermissionDenied("只有管理员可以授权付费 AI 分析。")
    decision = get_object_or_404(WheelDecision.objects.select_related(
        "underlying", "market_snapshot", "technical_snapshot", "event_snapshot", "family",
    ), pk=pk, family=_request_family(request))
    if request.POST.get("confirm_public_ai") != "yes":
        return HttpResponseBadRequest("须明确同意公开数据范围及本次费用上限。")
    try:
        consent = signing.loads(request.POST.get("consent_token", ""), salt="wheel-ai-consent-v1", max_age=7200)
        packet = build_advice_packet(decision, list(decision.candidates.select_related("option_quote")))["packet"]
        provider, config = provider_configuration()
        if (consent.get("family") != decision.family_id or consent.get("decision") != decision.pk
                or consent.get("input_hash") != packet["input_hash"] or consent.get("config_hash") != config["fingerprint"]):
            raise AdviceError("预览证据或模型配置已变化，请刷新页面重新核对。")
        analysis = enqueue_advice(decision=decision, user=request.user, packet=packet,
            provider=provider, config=config, nonce=consent["nonce"])
    except (signing.BadSignature, KeyError):
        return HttpResponseBadRequest("确认凭证失效，请返回建议页刷新后重试。")
    except AdviceError as exc:
        return HttpResponseBadRequest(str(exc))
    response = HttpResponseRedirect(reverse("option_wheel:advice_preview", args=[pk]) + f"?request={analysis.pk}")
    response.status_code = 303
    return response
