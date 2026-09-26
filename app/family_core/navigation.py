from urllib.parse import urlsplit

from django.urls import Resolver404, resolve


def return_url(request, fallback=None):
    """Accept only resolvable local paths, never remote URLs or the current page."""
    target = request.POST.get("return_to") or request.GET.get("return_to", "")
    if not target.startswith("/") or target.startswith("//") or "\\" in target:
        return fallback
    parsed = urlsplit(target)
    if parsed.netloc or parsed.scheme or parsed.path == request.path or any(ord(c) < 32 for c in target):
        return fallback
    try:
        match = resolve(parsed.path)
    except Resolver404:
        return fallback
    if match.app_name not in {"ledger", "portfolio", "knowledge", "notes", "investment_research", "option_wheel", "dashboard"}:
        return fallback
    return target
