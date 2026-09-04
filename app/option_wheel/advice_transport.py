"""Isolated, time-limited by parent; only contacts the approved DeepSeek endpoint."""
import json
import os
import sys
from urllib.request import Request, HTTPRedirectHandler, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError("AI endpoint redirects are not allowed")


def call_deepseek(request):
    from .advice import validate_advice_result
    from .advice_jobs import provider_configuration, check_request_cost
    _, config = provider_configuration(request.provider)
    if config["fingerprint"] != request.scope["config_hash"]:
        raise ValueError("configuration changed")
    check_request_cost(request.sanitized_input, config)
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
    result = validate_advice_result(json.loads(choice["message"]["content"]), request.sanitized_input)
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
