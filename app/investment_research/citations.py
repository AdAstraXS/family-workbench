"""投研原文引用：固定正文版本与字符区间，不随最新正文漂移。"""
import hashlib

from django.http import Http404


MAX_QUOTE_CHARS = 500


def quote_digest(quote):
    return hashlib.sha256(quote.encode("utf-8")).hexdigest()


def locate_quote(version, quote):
    quote = str(quote or "").strip()
    if not quote or len(quote) > MAX_QUOTE_CHARS:
        raise ValueError("引文应为 1–500 字的原文。")
    start = version.content_text.find(quote)
    if start < 0:
        raise ValueError("这段文字与当前正文版本不一致。")
    if version.content_text.find(quote, start + 1) >= 0:
        raise ValueError("这段文字在正文中出现多次，请复制更长的原文以明确位置。")
    return start, start + len(quote), quote_digest(quote)


def resolve_quote(version, start, end, digest):
    try:
        start, end = int(start), int(end)
    except (TypeError, ValueError) as exc:
        raise Http404("引文位置无效。") from exc
    if start < 0 or end > len(version.content_text) or not 0 < end - start <= MAX_QUOTE_CHARS:
        raise Http404("引文位置无效。")
    quote = version.content_text[start:end]
    if quote_digest(quote) != digest:
        raise Http404("引文与正文版本不一致。")
    return version.content_text[:start], quote, version.content_text[end:]
