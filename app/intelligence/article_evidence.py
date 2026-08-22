import hashlib
import re
from dataclasses import dataclass
from html.parser import HTMLParser

from django.utils import timezone

from .http_client import SafeHttpError, fetch_with_retries
from .models import SourceItem


EXTRACTION_VERSION = "public-evidence-v1"
MAX_EVIDENCE_PARAGRAPHS = 4
MAX_PARAGRAPH_CHARACTERS = 1200
MAX_EVIDENCE_CHARACTERS = 5000
MIN_ARTICLE_CHARACTERS = 280
BLOCK_TAGS = {"p", "h1", "h2", "h3", "blockquote", "li"}
SKIP_TAGS = {
    "script", "style", "noscript", "svg", "canvas", "form", "button",
    "nav", "footer", "header", "aside",
}
PAYWALL_MARKERS = (
    "subscribe to continue", "subscription required", "sign in to continue",
    "log in to continue", "already a subscriber", "premium content",
    "订阅后继续阅读", "登录后继续阅读", "会员专享", "付费阅读",
)


@dataclass(frozen=True)
class ArticleEvidenceResult:
    status: str
    reason: str
    evidence: str = ""
    content_hash: str = ""


class _ArticleTextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip_depth = 0
        self.preferred_depth = 0
        self.current = None
        self.current_preferred = False
        self.blocks = []

    def handle_starttag(self, tag, attrs):
        tag = tag.casefold()
        if tag in SKIP_TAGS:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag in {"article", "main"}:
            self.preferred_depth += 1
        if tag in BLOCK_TAGS:
            self._flush()
            self.current = []
            self.current_preferred = bool(self.preferred_depth)

    def handle_endtag(self, tag):
        tag = tag.casefold()
        if tag in SKIP_TAGS:
            self.skip_depth = max(0, self.skip_depth - 1)
            return
        if self.skip_depth:
            return
        if tag in BLOCK_TAGS:
            self._flush()
        if tag in {"article", "main"}:
            self.preferred_depth = max(0, self.preferred_depth - 1)

    def handle_data(self, data):
        if not self.skip_depth and self.current is not None:
            self.current.append(data)

    def close(self):
        super().close()
        self._flush()

    def _flush(self):
        if self.current is None:
            return
        text = re.sub(r"\s+", " ", " ".join(self.current)).strip()
        if len(text) >= 35:
            self.blocks.append((text, self.current_preferred))
        self.current = None
        self.current_preferred = False


def _decode_html(body):
    prefix = body[:4096]
    match = re.search(br"charset\s*=\s*['\"]?([A-Za-z0-9._-]+)", prefix, re.I)
    encodings = [match.group(1).decode("ascii", "ignore")] if match else []
    encodings.extend(["utf-8", "gb18030"])
    for encoding in encodings:
        try:
            return body.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode("utf-8", "replace")


def _terms(value):
    normalized = (value or "").casefold()
    latin = {token for token in re.findall(r"[a-z0-9]+", normalized) if len(token) >= 3}
    cjk = set(re.findall(r"[\u3400-\u9fff]{2,}", normalized))
    return latin | cjk


def extract_public_article_evidence(body, *, title="", subject_names=()):
    html = _decode_html(body)
    parser = _ArticleTextParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        return ArticleEvidenceResult(
            SourceItem.ARTICLE_FAILED,
            "网页 HTML 无法安全解析。",
        )
    preferred = [text for text, is_preferred in parser.blocks if is_preferred]
    paragraphs = preferred if sum(map(len, preferred)) >= MIN_ARTICLE_CHARACTERS else [
        text for text, _is_preferred in parser.blocks
    ]
    deduplicated = []
    seen = set()
    for paragraph in paragraphs:
        normalized = re.sub(r"\s+", " ", paragraph).strip()
        key = normalized.casefold()
        if key not in seen:
            seen.add(key)
            deduplicated.append(normalized)
    full_text = "\n".join(deduplicated)
    lower_lead = full_text[:3000].casefold()
    if any(marker in lower_lead for marker in PAYWALL_MARKERS) and (
        len(full_text) < 3000 or any(marker in lower_lead[:500] for marker in PAYWALL_MARKERS)
    ):
        return ArticleEvidenceResult(
            SourceItem.ARTICLE_BLOCKED,
            "页面要求登录、订阅或付费，未尝试绕过访问限制。",
        )
    if len(full_text) < MIN_ARTICLE_CHARACTERS:
        return ArticleEvidenceResult(
            SourceItem.ARTICLE_METADATA_ONLY,
            "公开页面没有足够的可核查正文，继续使用标题和订阅简介。",
        )

    query_terms = _terms(" ".join([title, *subject_names]))
    scored = []
    for index, paragraph in enumerate(deduplicated):
        overlap = len(query_terms & _terms(paragraph))
        score = overlap * 100 + min(len(paragraph), 600) - index
        scored.append((score, index, paragraph))
    selected = sorted(scored, reverse=True)[:MAX_EVIDENCE_PARAGRAPHS]
    if query_terms and not any(score >= 100 for score, _index, _paragraph in selected):
        selected = scored[:MAX_EVIDENCE_PARAGRAPHS]
    selected.sort(key=lambda row: row[1])
    snippets = []
    for number, (_score, _index, paragraph) in enumerate(selected, start=1):
        paragraph = paragraph[:MAX_PARAGRAPH_CHARACTERS].rstrip()
        candidate = f"[P{number}] {paragraph}"
        if len("\n\n".join([*snippets, candidate])) > MAX_EVIDENCE_CHARACTERS:
            break
        snippets.append(candidate)
    evidence = "\n\n".join(snippets)
    if not evidence:
        return ArticleEvidenceResult(
            SourceItem.ARTICLE_METADATA_ONLY,
            "公开页面未提取到符合最小内容边界的证据段落。",
        )
    content_hash = hashlib.sha256(full_text.encode("utf-8")).hexdigest()
    return ArticleEvidenceResult(
        SourceItem.ARTICLE_EXTRACTED,
        f"已从公开页面提取 {len(snippets)} 段必要证据；未保存完整正文或图片。",
        evidence=evidence,
        content_hash=content_hash,
    )


def fetch_article_evidence(item):
    if not item.source.article_fetch_enabled:
        return ArticleEvidenceResult(
            SourceItem.ARTICLE_NOT_REQUESTED,
            "该信源配置为只使用订阅元数据。",
        )
    if not item.canonical_url:
        result = ArticleEvidenceResult(
            SourceItem.ARTICLE_METADATA_ONLY,
            "来源条目没有公开原文链接，继续使用标题和订阅简介。",
        )
    else:
        try:
            response = fetch_with_retries(
                item.canonical_url,
                headers={"Accept": "text/html, application/xhtml+xml;q=0.9, */*;q=0.1"},
            )
            subject_names = list(
                item.source.topics.filter(is_active=True).values_list("display_name", flat=True)
            )
            result = extract_public_article_evidence(
                response.body,
                title=item.title,
                subject_names=subject_names,
            )
        except SafeHttpError as exc:
            blocked = exc.code in {"http_401", "http_402", "http_403"}
            result = ArticleEvidenceResult(
                SourceItem.ARTICLE_BLOCKED if blocked else SourceItem.ARTICLE_FAILED,
                (
                    "公开页面拒绝访问，未尝试登录、Cookie 或付费绕过。"
                    if blocked
                    else exc.safe_message
                ),
            )

    item.article_fetch_status = result.status
    item.article_fetch_reason = result.reason[:500]
    item.article_fetched_at = timezone.now()
    item.article_extraction_version = EXTRACTION_VERSION
    item.article_evidence = result.evidence
    item.article_content_hash = result.content_hash
    if result.status == SourceItem.ARTICLE_EXTRACTED:
        item.content_depth = SourceItem.DEPTH_PUBLIC_ARTICLE
    item.save(
        update_fields=[
            "article_fetch_status", "article_fetch_reason", "article_fetched_at",
            "article_extraction_version", "article_evidence", "article_content_hash",
            "content_depth", "updated_at",
        ]
    )
    return result
