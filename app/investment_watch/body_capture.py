"""Single-page capture, immutable source text and atomic per-company daily reservations."""

import gzip
import hashlib
import json
import os
from datetime import timedelta
from urllib.error import HTTPError
from urllib.request import Request, HTTPRedirectHandler, build_opener
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from family_core.models import Family
from intelligence.http_client import validate_public_http_url, SafeHttpError
from .models import BodySnapshot, BodyAttempt, WatchPipelineState, ResearchCandidate, BudgetReceipt
from .screening import authorized_provider, latest_screening, eligible, chosen
from .services import WatchError, candidate_stale, clean_url

DAILY_BODY_LIMIT = 3
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


def firecrawl_key():
    key_env = getattr(settings, "INVESTMENT_WATCH_FIRECRAWL_KEY_ENV", "FIRECRAWL_API_KEY")
    return os.getenv(key_env, "") or getattr(settings, "KNOWLEDGE_FIRECRAWL_API_KEY", "")


class BodyQuotaExhausted(WatchError):
    pass


def china_day():
    return timezone.now().astimezone(ZoneInfo("Asia/Shanghai")).date()


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise WatchError("正文服务发生重定向，停止本次请求。")


def capture_transport(request, timeout):
    with build_opener(NoRedirect()).open(request, timeout=timeout) as response:
        body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise WatchError("正文服务响应超过 2 MB，停止处理。")
        return body


@transaction.atomic
def reserve_body(candidate):
    """Family lock covers all owners and sources monitoring the same security."""
    Family.objects.select_for_update().get(pk=candidate.dossier.family_id)
    existing = BodyAttempt.objects.filter(family=candidate.dossier.family,
        security=candidate.dossier.security, material_version=candidate.material_version).first()
    if existing:
        return existing, False
    day = china_day()
    used = BodyAttempt.objects.filter(family=candidate.dossier.family,
        security=candidate.dossier.security, day=day).count()
    if used >= DAILY_BODY_LIMIT:
        raise BodyQuotaExhausted("该公司今日正文名额已用完（3 篇）；候选保留，等待下一轮检查。")
    return BodyAttempt.objects.create(family=candidate.dossier.family, security=candidate.dossier.security,
        candidate=candidate, material_version=candidate.material_version, day=day,
        message="已预留正文名额；请求中断或失败均占用名额，不自动重试。"), True


def _snapshot(version, text, raw, url, method="firecrawl-v2"):
    return BodySnapshot.objects.get_or_create(material_version=version, defaults={
        "text": text, "content_hash": hashlib.sha256(text.encode()).hexdigest(),
        "raw_gzip": gzip.compress(raw), "source_url": url, "method": method})[0]


def capture_question_body(candidate):
    """Reuse the archive/quota for new question workflows; public HTML only.

    This path never buys Firecrawl or calls a second screening model. Sources
    requiring login, scripts or a paywall stay explicit unread leads.
    """
    from investment_research.question_ai import consent_for
    from investment_research.models import ResearchAutoDigestConsent
    from investment_research.preparation import authorize
    from investment_research.research_ai import ResearchAiError
    from investment_research.ir_extraction import extract_material
    from knowledge.web_fetch import public_request, WebCaptureError
    from types import SimpleNamespace
    candidate = ResearchCandidate.objects.select_related('dossier__owner__user', 'dossier__security',
        'material_version__material__source', 'material_version__official_version').get(pk=candidate.pk)
    dossier, version = candidate.dossier, candidate.material_version
    if not getattr(settings, 'INVESTMENT_WATCH_BODY_ENABLED', False):
        raise WatchError('新闻正文采集尚未启用，保留标题与摘要线索。')
    try:
        authorize(dossier.owner, dossier)
        consent = ResearchAutoDigestConsent.objects.filter(dossier=dossier, revoked_at__isnull=True).select_related('provider').first()
        if not consent:
            raise ResearchAiError('自动问题分析未授权。')
        consent_for(dossier, consent.provider)
    except ResearchAiError as exc:
        raise WatchError(str(exc)) from exc
    if not dossier.owner.user.is_active or version.material.source.family_id != dossier.family_id:
        raise WatchError('成员或来源权限已变化，停止正文获取。')
    if version.status != 'active':
        raise WatchError('来源已更正或撤回，保留版本信息供核查。')
    saved = BodySnapshot.objects.filter(material_version=version).first()
    if saved:
        return saved
    if version.official_version_id and version.official_version.content_text.strip():
        text = version.official_version.content_text
        return _snapshot(version, text, text.encode(), version.url, 'official-archive')
    # Never consume historical material when automatic analysis is first enabled.
    if version.found_at < consent.authorized_at or version.found_at < timezone.now() - timedelta(hours=48):
        raise WatchError('授权前的历史候选不自动补抓正文。')
    same = BodySnapshot.objects.filter(material_version__material__source__family_id=dossier.family_id,
        material_version__url=version.url, material_version__content_hash=version.content_hash).first()
    if same:
        return _snapshot(version, same.text, gzip.decompress(bytes(same.raw_gzip)), same.source_url, 'cached')
    attempt, created = reserve_body(candidate)
    if not created:
        if attempt.snapshot_id:
            return attempt.snapshot
        raise WatchError('正文请求已有记录，失败或中断不自动重试。')
    try:
        # Consent can be revoked between the quota reservation and network request.
        dossier.refresh_from_db()
        consent_for(dossier, consent.provider)
        version.refresh_from_db()
        if version.status != 'active':
            raise WatchError('来源已更正或撤回，停止正文请求。')
        raw, content_type = public_request(clean_url(version.url), limit=MAX_RESPONSE_BYTES, timeout=20)
        extracted = extract_material(SimpleNamespace(url=version.url, raw=raw, content_type=content_type.split(';')[0].strip()))
        if len(extracted['text']) < 1500 and any(marker in extracted['text'].casefold() for marker in (
                'subscribe to continue', 'sign in to continue', 'subscription required', '订阅后阅读', '登录后阅读')):
            raise WatchError('来源只返回登录或订阅提示，保留摘要线索和原文链接。')
        with transaction.atomic():
            saved = _snapshot(version, extracted['text'], raw, version.url, 'public-original')
            attempt.snapshot, attempt.status = saved, 'completed'
            attempt.message = '已保存公开原文；不含登录或付费页面，事实仍需核查。'
            attempt.save(update_fields=['snapshot', 'status', 'message', 'updated_at'])
        return saved
    except Exception as exc:
        message = str(exc) if isinstance(exc, (WatchError, ResearchAiError, WebCaptureError)) else '公开原文暂时无法读取，保留摘要线索。'
        attempt.status, attempt.message = 'failed', message[:500]
        attempt.save(update_fields=['status', 'message', 'updated_at'])
        raise WatchError(attempt.message) from exc


def capture_body(candidate, *, transport=None, url_validator=None):
    if not getattr(settings, "INVESTMENT_WATCH_BODY_ENABLED", False):
        raise WatchError("正文采集尚未启用。")
    candidate = ResearchCandidate.objects.select_related("dossier__owner__family", "dossier__security",
        "dossier__current_revision", "material_version__material__source", "material_version__official_version").get(pk=candidate.pk)
    provider = authorized_provider(candidate.dossier)
    state, _ = WatchPipelineState.objects.get_or_create(family=candidate.dossier.family)
    screening = latest_screening(candidate, provider)
    if not eligible(candidate, state) or not chosen(candidate, screening):
        raise WatchError("候选未通过当前版本的初筛，不能抓取正文。")
    version = candidate.material_version
    if version.material.source.family_id != candidate.dossier.family_id:
        raise WatchError("正文来源不属于本家庭。")
    saved = BodySnapshot.objects.filter(material_version=version).first()
    if saved:
        return saved
    # Official documents already have frozen text; reuse it without a cloud request.
    if version.official_version_id and version.official_version.content_text.strip():
        text = version.official_version.content_text
        return _snapshot(version, text, text.encode(), version.url, "official-archive")
    # Reuse an identical URL and metadata snapshot, never a changed source version.
    same = BodySnapshot.objects.filter(material_version__material__source__family_id=candidate.dossier.family_id,
        material_version__url=version.url, material_version__content_hash=version.content_hash).first()
    if same:
        return _snapshot(version, same.text, gzip.decompress(bytes(same.raw_gzip)), same.source_url, "cached")
    from investment_research.research_ai import research_provider_policy, _cost
    from .budget import budget_status, amount
    policy = research_provider_policy(provider)
    budget = budget_status(candidate.dossier.family)
    # Avoid buying a body when even the minimum analysis reservation cannot fit.
    minimum = _cost(0, min(policy["max_output_tokens"], 4000), policy) * amount(provider.extra_data.get("watch_usd_cny", 0))
    if (budget["daily"] + minimum > budget["daily_limit"]
            or budget["monthly"] + minimum > budget["monthly_limit"]
            or BudgetReceipt.objects.filter(family=candidate.dossier.family, status="overrun").exists()):
        raise WatchError("模型额度不足，暂不新增正文请求；重要候选保留。")
    api_key = firecrawl_key()
    if not api_key:
        raise WatchError("Firecrawl 密钥尚未配置，未发送正文请求。")
    from .capture_account import check_credits
    check_credits()
    try:
        url = (url_validator or validate_public_http_url)(clean_url(version.url))
    except SafeHttpError as exc:
        raise WatchError(exc.safe_message) from None
    if url.split("?", 1)[0].casefold().endswith(".pdf"):
        raise WatchError("此流程仅采集网页；PDF 请使用已有官方资料归档。")
    attempt, created = reserve_body(candidate)
    if not created:
        if attempt.snapshot_id:
            return attempt.snapshot
        raise WatchError("本条正文请求已有记录，失败或用量未知时不自动重试，请查看运行记录。")
    try:
        if candidate_stale(candidate):
            raise WatchError("材料或研究判断已更新，停止正文请求。")
        body = json.dumps({"url": url, "formats": ["markdown"], "onlyMainContent": True,
                           "onlyCleanContent": False, "parsers": [], "maxAge": 0,
                           "timeout": 60000}).encode()
        request = Request("https://api.firecrawl.dev/v2/scrape", data=body,
                          headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, method="POST")
        raw = (transport or capture_transport)(request, timeout=75)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise WatchError("正文响应超过 2 MB。")
        value = json.loads(raw)
        data = value.get("data", {})
        text = data.get("markdown") if isinstance(data, dict) else None
        metadata = data.get("metadata", {}) if isinstance(data, dict) else {}
        if (value.get("success") is not True or not isinstance(text, str) or len(text.strip()) < 120
                or not isinstance(metadata, dict) or metadata.get("statusCode", 200) != 200
                or data.get("warning") or metadata.get("error")
                or "pdf" in str(metadata.get("contentType", "")).casefold()):
            raise WatchError("未取得可用网页正文；可能存在付费墙、访问限制或不完整响应。")
        final_url = clean_url(metadata.get("sourceURL") or url)
        (url_validator or validate_public_http_url)(final_url)
        # Block short sign-in/paywall stubs rather than passing them off as an article.
        if len(text) < 1500 and any(marker in text.casefold() for marker in (
                "subscribe to continue", "sign in to continue", "subscription required", "订阅后阅读", "登录后阅读")):
            raise WatchError("来源只返回登录或订阅提示，保留原文链接。")
        with transaction.atomic():
            snapshot = _snapshot(version, text, raw, final_url)
            attempt.snapshot = snapshot
            attempt.status = "completed"
            attempt.message = "已保存网页正文；抓取成功不等于来源事实已核实。"
            attempt.save(update_fields=["snapshot", "status", "message", "updated_at"])
        return snapshot
    except Exception as exc:
        if isinstance(exc, HTTPError) and exc.code == 402:
            from .capture_account import exhausted
            exhausted()
        attempt.status = "failed"
        attempt.message = (str(exc) if isinstance(exc, WatchError) else
                           f"正文服务返回 HTTP {exc.code}。" if isinstance(exc, HTTPError) else
                           "正文请求未完成或返回无效；保留名额，不自动重试。")[:500]
        attempt.save(update_fields=["status", "message", "updated_at"])
        raise WatchError(attempt.message) from None
