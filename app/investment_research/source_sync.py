"""官方材料同步服务（M2A-2：SEC 单证券元数据同步；M2A-3：MSFT IR 正文同步；
M2A-4：统一同步编排与计数口径 created/updated/unchanged/failed）。

SEC 只同步元数据，content_text 保持空；Microsoft IR 同步保存可核查的
标题、日期、FY/Q、规范化 URL 与纯文本正文。
client 注入（duck typing），离线测试用 stub。
"""
import hashlib
import logging

from django.conf import settings
from django.db import transaction
from django.db.models.functions import Upper
from django.utils import timezone

from portfolio.models import Security

from .models import (
    OfficialResearchDocument,
    ResearchSourceState,
    SOURCE_MICROSOFT_IR,
    SOURCE_SEC,
)
from .providers.microsoft_ir import (
    MicrosoftIRClient,
    MicrosoftIRError,
    discover_earnings_links,
)
from .providers.sec import SecClient, SecClientError, filing_url

logger = logging.getLogger(__name__)

LAST_ERROR_MAX_LENGTH = 2000


class SecSyncError(ValueError):
    """SEC 同步前置条件不满足（例如非美股/非普通股）；message 可直接展示。"""


def sync_sec_documents(*, security, client, max_documents=20):
    """同步单个美股证券的 SEC 元数据，按 accession 幂等。

    返回计数 dict：{"created": n, "updated": n, "unchanged": n, "failed": n}。
    业务字段（document_type/title/source_url/published_at/period_end/metadata）
    有变化才计 updated；仅 fetched_at 刷新计 unchanged。
    致命错误（拉取失败）抛 SecClientError，同时更新 state 的
    checked/error 并保留既有 success 与文档。
    """
    if isinstance(max_documents, bool) or not isinstance(max_documents, int):
        raise SecSyncError(
            f"max_documents 应为 1..100 的整数，当前为 {max_documents!r}。"
        )
    if not 1 <= max_documents <= 100:
        raise SecSyncError(f"max_documents 应为 1..100，当前为 {max_documents!r}。")
    if security.market != "US":
        raise SecSyncError(
            f"SEC 同步目前仅支持美股（US），当前市场为 {security.market}。"
        )
    if security.asset_type != Security.TYPE_STOCK:
        raise SecSyncError(
            f"SEC 同步目前仅支持普通股，当前品种为 {security.asset_type}。"
        )

    now = timezone.now()
    state, _ = ResearchSourceState.objects.get_or_create(
        security=security, source=SOURCE_SEC
    )

    cik = state.external_company_id
    if not cik:
        try:
            cik = client.resolve_cik(security.symbol)
        except SecClientError as exc:
            # 首次 CIK 解析失败同样记 checked/error，保留既有 success 与文档
            _record_failure(state, now, exc)
            raise
        state.external_company_id = cik
        state.save(update_fields=["external_company_id", "updated_at"])

    try:
        records = client.get_filings(cik)[:max_documents]
    except SecClientError as exc:
        _record_failure(state, now, exc)
        raise

    counters = {"created": 0, "updated": 0, "unchanged": 0, "failed": 0}
    errors = []
    for record in records:
        try:
            status = _upsert_document(
                security=security, cik=cik, record=record, now=now
            )
        except Exception as exc:  # 单文档失败不影响已成功文档
            counters["failed"] += 1
            errors.append(f"{record['accession']}: {exc}")
            continue
        counters[status] += 1

    state.last_checked_at = now
    succeeded = counters["created"] + counters["updated"] + counters["unchanged"]
    if succeeded > 0 or not records:
        state.last_success_at = now
    if errors:
        state.last_error = "; ".join(errors)[:LAST_ERROR_MAX_LENGTH]
    else:
        state.last_error = None
    state.save(
        update_fields=[
            "last_checked_at",
            "last_success_at",
            "last_error",
            "updated_at",
        ]
    )
    return counters


def _record_failure(state, now, exc):
    state.last_checked_at = now
    state.last_error = str(exc)[:LAST_ERROR_MAX_LENGTH]
    state.save(update_fields=["last_checked_at", "last_error", "updated_at"])


def _upsert_document(*, security, cik, record, now):
    url = filing_url(cik, record["accession"], record["primary_document"])
    metadata = {
        "cik": cik,
        "form": record["form"],
        "amendment": record["form"].endswith("/A"),
        "filing_date": record["filing_date"].isoformat(),
        "report_date": (
            record["report_date"].isoformat() if record["report_date"] else None
        ),
    }
    with transaction.atomic():
        document, created = OfficialResearchDocument.objects.get_or_create(
            source=SOURCE_SEC,
            external_id=record["accession"],
            defaults={
                "security": security,
                "document_type": record["document_type"],
                "title": record["title"],
                "source_url": url,
                "published_at": record["filing_date"],
                "period_end": record["report_date"],
                "fetched_at": now,
                "metadata": metadata,
            },
        )
        if not created:
            if document.security_id != security.pk:
                # 数据冲突：同一 accession 已归属另一证券，保留原归属，
                # 由调用方计 failed 并写短错误，不静默改挂。
                raise ValueError(
                    f"accession {record['accession']} 已属于另一证券，保留原归属。"
                )
            changed = (
                document.document_type != record["document_type"]
                or document.title != record["title"]
                or document.source_url != url
                or document.published_at != record["filing_date"]
                or document.period_end != record["report_date"]
                or document.metadata != metadata
            )
            document.document_type = record["document_type"]
            document.title = record["title"]
            document.source_url = url
            document.published_at = record["filing_date"]
            document.period_end = record["report_date"]
            document.fetched_at = now
            document.metadata = metadata
            document.save(
                update_fields=[
                    "document_type",
                    "title",
                    "source_url",
                    "published_at",
                    "period_end",
                    "fetched_at",
                    "metadata",
                    "updated_at",
                ]
            )
            # 仅 fetched_at 刷新不算业务变化。
            return "unchanged" if not changed else "updated"
    return "created"


class MicrosoftIRSyncError(ValueError):
    """Microsoft IR 同步前置条件不满足（例如非 MSFT）；message 可直接展示。"""


def sync_microsoft_ir_documents(*, security, client, max_documents=20):
    """同步 MSFT 的 Microsoft IR 财报页（标题/日期/FY-Q/URL/纯文本正文）。

    按规范化 URL（external_id）幂等；单文档失败不影响其他文档。
    返回计数 dict：{"created": n, "updated": n, "unchanged": n, "failed": n}。
    业务字段（document_type/title/source_url/published_at/content_sha256/metadata）
    有变化才计 updated；仅 fetched_at 刷新计 unchanged。
    致命错误（首页抓取/发现失败）抛 MicrosoftIRError，同时更新 state 的
    checked/error 并保留既有 success 与旧正文。
    """
    if isinstance(max_documents, bool) or not isinstance(max_documents, int):
        raise MicrosoftIRSyncError(
            f"max_documents 应为 1..100 的整数，当前为 {max_documents!r}。"
        )
    if not 1 <= max_documents <= 100:
        raise MicrosoftIRSyncError(f"max_documents 应为 1..100，当前为 {max_documents!r}。")
    if security.symbol.upper() != "MSFT":
        raise MicrosoftIRSyncError(
            f"Microsoft IR 同步目前仅支持 MSFT，当前代码为 {security.symbol}。"
        )

    now = timezone.now()
    state, _ = ResearchSourceState.objects.get_or_create(
        security=security, source=SOURCE_MICROSOFT_IR
    )

    try:
        home_html = client.fetch_home()
        links = discover_earnings_links(home_html)
    except MicrosoftIRError as exc:
        _record_failure(state, now, exc)
        raise
    if not links:
        exc = MicrosoftIRError(
            "首页未发现任何财报页链接，页面结构可能已变化。"
        )
        _record_failure(state, now, exc)
        raise exc

    counters = {"created": 0, "updated": 0, "unchanged": 0, "failed": 0}
    errors = []
    for link in links[:max_documents]:
        try:
            status = _upsert_microsoft_document(
                security=security, link=link, client=client, now=now
            )
        except Exception as exc:  # 单文档失败不影响已成功文档
            counters["failed"] += 1
            errors.append(f"{link['url']}: {exc}")
            continue
        counters[status] += 1

    state.last_checked_at = now
    succeeded = counters["created"] + counters["updated"] + counters["unchanged"]
    if succeeded > 0 or not links:
        state.last_success_at = now
    if errors:
        state.last_error = "; ".join(errors)[:LAST_ERROR_MAX_LENGTH]
    else:
        state.last_error = None
    state.save(
        update_fields=[
            "last_checked_at",
            "last_success_at",
            "last_error",
            "updated_at",
        ]
    )
    return counters


def _upsert_microsoft_document(*, security, link, client, now):
    page = client.fetch_earnings_page(link["url"])
    content = page["content_text"]
    content_sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
    metadata = {"fy": link["fy"], "quarter": link["quarter"]}
    with transaction.atomic():
        document, created = OfficialResearchDocument.objects.get_or_create(
            source=SOURCE_MICROSOFT_IR,
            external_id=link["url"],
            defaults={
                "security": security,
                "document_type": "earnings_release",
                "title": page["title"],
                "source_url": link["url"],
                "published_at": page["published_at"],
                "content_text": content,
                "content_sha256": content_sha,
                "fetched_at": now,
                "metadata": metadata,
            },
        )
        if not created:
            if document.security_id != security.pk:
                # 数据冲突：同一 URL 已归属另一证券，保留原归属，
                # 由调用方计 failed 并写短错误，不静默改挂。
                raise ValueError(
                    f"URL {link['url']} 已属于另一证券，保留原归属。"
                )
            changed = (
                document.document_type != "earnings_release"
                or document.title != page["title"]
                or document.source_url != link["url"]
                or document.published_at != page["published_at"]
                or document.content_sha256 != content_sha
                or document.metadata != metadata
            )
            document.document_type = "earnings_release"
            document.title = page["title"]
            document.source_url = link["url"]
            document.published_at = page["published_at"]
            document.content_text = content
            document.content_sha256 = content_sha
            document.fetched_at = now
            document.metadata = metadata
            document.save(
                update_fields=[
                    "document_type",
                    "title",
                    "source_url",
                    "published_at",
                    "content_text",
                    "content_sha256",
                    "fetched_at",
                    "metadata",
                    "updated_at",
                ]
            )
            # 仅 fetched_at 刷新不算业务变化。
            return "unchanged" if not changed else "updated"
    return "created"


# ---------------------------------------------------------------------------
# M2A-4：统一同步编排（供 sync_research_sources 管理命令与 NAS 定时任务调用）
# ---------------------------------------------------------------------------

SYNC_SOURCE_CHOICES = (SOURCE_SEC, SOURCE_MICROSOFT_IR)
_SHORT_REASON_MAX_LENGTH = 200


def _default_sec_client(security):
    """按 settings 构造真实 SEC client（测试可替换本工厂）。"""
    return SecClient(
        user_agent=settings.RESEARCH_SEC_USER_AGENT,
        timeout=settings.RESEARCH_SEC_TIMEOUT_SECONDS,
        max_response_bytes=settings.RESEARCH_SEC_MAX_RESPONSE_BYTES,
        rate_limit_per_second=settings.RESEARCH_SEC_RATE_LIMIT_PER_SECOND,
        max_retries=settings.RESEARCH_SEC_MAX_RETRIES,
        backoff_seconds=settings.RESEARCH_SEC_BACKOFF_SECONDS,
    )


def _default_microsoft_ir_client(security):
    """按 settings 构造真实 Microsoft IR client（测试可替换本工厂）。"""
    return MicrosoftIRClient(
        timeout_seconds=settings.RESEARCH_MICROSOFT_IR_TIMEOUT_SECONDS,
        max_response_bytes=settings.RESEARCH_MICROSOFT_IR_MAX_RESPONSE_BYTES,
        max_content_chars=settings.RESEARCH_MICROSOFT_IR_MAX_CONTENT_CHARS,
    )


def _short_reason(exc):
    """终端可展示的短原因：去换行、截断，不含响应体/正文。"""
    return " ".join(str(exc).split())[:_SHORT_REASON_MAX_LENGTH]


def sync_research_sources(
    *,
    symbols=None,
    sources=None,
    max_documents=20,
    sec_client_factory=None,
    microsoft_ir_client_factory=None,
):
    """统一同步编排：只同步已有研究档案的不同证券，同一证券只执行一次。

    参数在任何 provider 调用前校验（非法即抛 ValueError）：
    - symbols: None=全部有档案证券；否则代码列表（大小写不敏感精确匹配，
      未知代码抛错，不回退为全部）。
    - sources: None=两个来源；否则 SYNC_SOURCE_CHOICES 子集。
    - max_documents: 1..100 的整数。

    返回 (results, totals)：
    - results: 每证券每来源一条 {"security", "source",
      "status"("ok"/"skipped"/"failed"), "counters"(ok 时), "reason"}；
    - totals: {"created", "updated", "unchanged", "skipped", "failed"}。
    skipped 仅用于 Microsoft IR 对非 MSFT 的前置跳过（不建错误状态）；
    其他配置或同步错误计 failed。
    """
    if isinstance(max_documents, bool) or not isinstance(max_documents, int):
        raise ValueError(
            f"max_documents 应为 1..100 的整数，当前为 {max_documents!r}。"
        )
    if not 1 <= max_documents <= 100:
        raise ValueError(f"max_documents 应为 1..100，当前为 {max_documents!r}。")
    if sources is None:
        source_list = list(SYNC_SOURCE_CHOICES)
    else:
        source_list = list(dict.fromkeys(sources))
        for source in source_list:
            if source not in SYNC_SOURCE_CHOICES:
                raise ValueError(
                    f"未知来源：{source!r}，可选 {list(SYNC_SOURCE_CHOICES)}。"
                )

    if symbols is None:
        securities = list(
            Security.objects.filter(research_dossiers__isnull=False)
            .distinct()
            .order_by("symbol", "pk")
        )
    else:
        wanted = {str(symbol).upper() for symbol in symbols}
        securities = list(
            Security.objects.filter(
                research_dossiers__isnull=False
            )
            .annotate(symbol_upper=Upper("symbol"))
            .filter(symbol_upper__in=wanted)
            .distinct()
            .order_by("symbol", "pk")
        )
        found = {security.symbol.upper() for security in securities}
        missing = sorted(wanted - found)
        if missing:
            raise ValueError(f"以下代码没有研究档案：{'、'.join(missing)}。")

    results = []
    totals = {"created": 0, "updated": 0, "unchanged": 0, "skipped": 0, "failed": 0}
    for security in securities:
        for source in source_list:
            entry = _sync_one(
                security=security,
                source=source,
                max_documents=max_documents,
                sec_client_factory=sec_client_factory,
                microsoft_ir_client_factory=microsoft_ir_client_factory,
            )
            results.append(entry)
            if entry["status"] == "ok":
                counters = entry["counters"]
                totals["created"] += counters["created"]
                totals["updated"] += counters["updated"]
                totals["unchanged"] += counters["unchanged"]
                totals["failed"] += counters["failed"]
            elif entry["status"] == "skipped":
                totals["skipped"] += 1
            else:
                totals["failed"] += 1
    return results, totals


def _sync_one(
    *,
    security,
    source,
    max_documents,
    sec_client_factory,
    microsoft_ir_client_factory,
):
    """单个（证券，来源）组合的同步；异常不向上传播，转为 status/reason。"""
    base = {"security": security.symbol, "source": source}
    if source == SOURCE_SEC:
        factory = sec_client_factory or _default_sec_client
        sync_fn = sync_sec_documents
        skipped_exc = None
    elif source == SOURCE_MICROSOFT_IR:
        factory = microsoft_ir_client_factory or _default_microsoft_ir_client
        sync_fn = sync_microsoft_ir_documents
        skipped_exc = MicrosoftIRSyncError
    else:  # 理论上已由 sync_research_sources 校验
        raise ValueError(f"未知来源：{source!r}。")

    try:
        client = factory(security)
    except Exception as exc:  # 配置错误（例如 UA 未配置）计 failed，不静默跳过
        logger.exception("构造 %s client 失败：%s", source, security.symbol)
        return {**base, "status": "failed", "counters": None, "reason": _short_reason(exc)}

    try:
        counters = sync_fn(
            security=security, client=client, max_documents=max_documents
        )
    except Exception as exc:
        if skipped_exc is not None and isinstance(exc, skipped_exc):
            # 前置条件不满足（例如 MSFT IR 对非 MSFT）：skipped，不建错误状态
            return {**base, "status": "skipped", "counters": None, "reason": _short_reason(exc)}
        logger.exception("%s 同步异常：%s", source, security.symbol)
        return {**base, "status": "failed", "counters": None, "reason": _short_reason(exc)}
    return {**base, "status": "ok", "counters": counters, "reason": None}
