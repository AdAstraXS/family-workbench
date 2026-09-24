"""Isolated, time-limited by parent; only contacts the approved DeepSeek endpoint."""
import json
import os
import sys
from urllib.request import Request, HTTPRedirectHandler, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError("AI endpoint redirects are not allowed")


def call_deepseek(request):
    from .advice import SCHEMA as LEGACY_SCHEMA, validate_advice_result
    from .advice_jobs import provider_configuration, check_request_cost
    from .screen_advice import SCHEMA as SCREEN_SCHEMA, validate_result
    from .screen_advice_jobs import estimated_cost, screen_provider_configuration
    if request.analysis_type == SCREEN_SCHEMA:
        _, config = screen_provider_configuration(request.provider)
        estimated_cost(request.sanitized_input, config)
        validator = validate_result
    elif request.analysis_type == LEGACY_SCHEMA:
        _, config = provider_configuration(request.provider)
        check_request_cost(request.sanitized_input, config)
        validator = validate_advice_result
    else:
        raise ValueError("unsupported advice schema")
    if config["fingerprint"] != request.scope["config_hash"]:
        raise ValueError("configuration changed")
    payload = {"model": config["model"], "thinking": {"type": "disabled"},
        "max_tokens": config["max_output_tokens"], "response_format": {"type": "json_object"},
        "messages": [{"role": "system", "content": request.prompt},
            {"role": "user", "content": json.dumps(request.sanitized_input, ensure_ascii=False)}]}
    http = Request("https://api.deepseek.com/chat/completions", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + os.environ[config["api_key_env_var"]]}, method="POST")
    with build_opener(NoRedirect()).open(http, timeout=45) as response:
        raw = response.read(262145)
    if len(raw) > 262144:
        raise ValueError("response too large")
    decoded = json.loads(raw)
    choice = decoded["choices"][0]
    if choice.get("finish_reason") != "stop":
        raise ValueError("incomplete response")
    result = validator(json.loads(choice["message"]["content"]), request.sanitized_input)
    return {"result": result, "usage": decoded.get("usage", {})}


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    import django
    django.setup()
    from ai_analysis.models import AiAnalysisRequest
    from .advice_jobs import MODULE
    request = AiAnalysisRequest.objects.select_related("provider").get(pk=int(sys.argv[1]), module=MODULE, status="pending")
    print(json.dumps(call_deepseek(request), ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Never expose raw vendor errors, bearer headers or credentials to task logs.
        sys.exit(1)
