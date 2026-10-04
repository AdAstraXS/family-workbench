"""Daily account-level credit observation; all page reads are cache-only."""
import hashlib
import json
from datetime import timedelta
from urllib.request import Request
from django.utils import timezone
from .models import CaptureAccountState


def cached_usage():
    from .body_capture import firecrawl_key
    key = firecrawl_key()
    return CaptureAccountState.objects.filter(key_hash=hashlib.sha256(key.encode()).hexdigest()).first() if key else None


def refresh_usage(*, transport=None):
    from .body_capture import firecrawl_key, capture_transport
    key = firecrawl_key()
    if not key:
        return None
    state, _ = CaptureAccountState.objects.get_or_create(key_hash=hashlib.sha256(key.encode()).hexdigest())
    now = timezone.now()
    if state.checked_at and state.checked_at > now - timedelta(hours=24):
        return state
    # Reserve the observation before networking, avoiding automatic retry storms.
    changed = CaptureAccountState.objects.filter(pk=state.pk, checked_at=state.checked_at).update(checked_at=now)
    if not changed:
        return state
    state.checked_at = now
    try:
        request = Request("https://api.firecrawl.dev/v2/team/credit-usage", headers={"Authorization": f"Bearer {key}"})
        value = json.loads((transport or capture_transport)(request, timeout=20))
        data = value.get("data", {})
        if value.get("success") is not True or any(type(data.get(k)) is not int or data[k] < 0 for k in ("remainingCredits", "planCredits")):
            raise ValueError("Invalid usage response")
        from django.utils.dateparse import parse_datetime
        reset = parse_datetime(data.get("billingPeriodEnd", ""))
        if reset and timezone.is_naive(reset):
            reset = timezone.make_aware(reset)
        state.remaining, state.plan_credits, state.resets_at = data["remainingCredits"], data["planCredits"], reset
        state.error = ""
    except Exception:
        state.error = "额度查询失败；保留上次结果，请查看 Firecrawl 账户。"
    state.save(update_fields=["checked_at", "remaining", "plan_credits", "resets_at", "error"])
    return state


def check_credits():
    from .services import WatchError
    state = cached_usage()
    if state and state.remaining == 0 and (not state.resets_at or state.resets_at > timezone.now()):
        raise WatchError("Firecrawl 账户额度已耗尽，保留候选并暂停新增正文请求。")


def exhausted():
    state = cached_usage()
    if state:
        state.remaining = 0
        state.resets_at = None
        state.error = "正文服务返回额度不足，暂停新增请求，等待额度检查。"
        state.save(update_fields=["remaining", "resets_at", "error"])
