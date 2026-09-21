"""按需取得 SEC 主 HTML，保留原件与不可变的可读正文版本。"""
import gzip
import hashlib
import re
from html.parser import HTMLParser

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import (
    DOC_TYPE_10K, DOC_TYPE_10Q, DOC_TYPE_8K,
    SOURCE_SEC, OfficialResearchContentVersion, OfficialResearchDocument,
    ResearchDossier,
)
from .providers.sec import ARCHIVES_BASE, SecDocumentUrlError, filing_url
from .services import DossierNotFound, ResearchValidationError, _require_writer
from .source_sync import _default_sec_client

EXTRACTOR_VERSION = "sec-html-v1"
MAX_CONTENT_CHARS = 2_000_000
_BLOCKS = frozenset({"p", "div", "section", "article", "h1", "h2", "h3", "h4", "h5", "h6", "br", "li", "tr"})
_IGNORED = frozenset({"script", "style", "noscript", "svg", "ix:hidden", "ix:header"})
_VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "wbr"})


class _ReadableHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.ignored = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if self.ignored or tag in _IGNORED or "hidden" in attrs or "display:none" in (attrs.get("style") or "").replace(" ", "").lower():
            if tag not in _VOID:
                self.ignored.append(tag)
            return
        if tag in _BLOCKS:
            self.parts.append("\n" if tag in {"br", "tr"} else "\n\n")
        elif tag in {"td", "th"}:
            self.parts.append(" | ")

    def handle_endtag(self, tag):
        if self.ignored:
            if tag in self.ignored:
                while self.ignored:
                    if self.ignored.pop() == tag:
                        break
        elif tag in _BLOCKS:
            self.parts.append("\n" if tag == "tr" else "\n\n")

    def handle_data(self, data):
        if not self.ignored:
            self.parts.append(data)


def extract_sec_html(raw):
    head = raw[:4096].lower()
    if b"<html" not in head and b"<!doctype html" not in head:
        raise ResearchValidationError("SEC 主文件不是可读取的 HTML。")
    match = re.search(rb"charset\s*=\s*['\"]?([a-z0-9_-]+)", head)
    charset = match.group(1).decode("ascii") if match else "utf-8"
    if charset not in {"utf-8", "utf8", "iso-8859-1", "latin-1", "windows-1252"}:
        raise ResearchValidationError("SEC HTML 编码尚不支持。")
    try:
        html = raw.decode(charset)
    except UnicodeError as exc:
        if match:
            raise ResearchValidationError("SEC HTML 解码失败。") from exc
        try:
            html = raw.decode("windows-1252")
        except UnicodeError as fallback_exc:
            raise ResearchValidationError("SEC HTML 解码失败。") from fallback_exc
    parser = _ReadableHTML()
    parser.feed(html)
    parser.close()
    lines = [re.sub(r"[ \t\r\f\v]+", " ", line).strip(" |") for line in "".join(parser.parts).splitlines()]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    if len(text) < 30 or len(text) > MAX_CONTENT_CHARS:
        raise ResearchValidationError("SEC 正文为空、过短或超过可读长度上限。")
    return text


def _verified_url(document):
    meta = document.metadata or {}
    cik = meta.get("cik")
    accession = document.external_id
    if not cik or not isinstance(accession, str):
        raise SecDocumentUrlError("SEC 文件缺少可核查的 CIK 或 accession。")
    try:
        cik_number = str(int(cik))
    except (ValueError, TypeError) as exc:
        raise SecDocumentUrlError("SEC 文件的 CIK 无效。") from exc
    base = f"{ARCHIVES_BASE}/{cik_number}/{accession.replace('-', '')}/"
    url = document.source_url
    if not url.startswith(base):
        raise SecDocumentUrlError("SEC 原文链接与文件身份不一致。")
    relative = url[len(base):]
    if not relative.lower().endswith(('.htm', '.html')):
        raise ResearchValidationError("这份 SEC 主文件不是 HTML，暂不能提取正文。")
    if filing_url(cik, accession, relative) != url:
        raise SecDocumentUrlError("SEC 原文链接未通过官方路径校验。")
    return url


def fetch_sec_document_content(*, actor, dossier_id, document_id, client=None):
    """返回 (版本, 是否新增)，失败时不改旧正文。"""
    _require_writer(actor)
    dossier = ResearchDossier.objects.filter(
        pk=dossier_id, owner=actor, family=actor.family,
    ).first()
    if dossier is None:
        raise DossierNotFound("档案不存在。")
    document = OfficialResearchDocument.objects.filter(
        pk=document_id, security=dossier.security,
    ).first()
    if document is None:
        raise DossierNotFound("资料不存在。")
    if document.source != SOURCE_SEC or document.document_type not in {DOC_TYPE_10K, DOC_TYPE_10Q, DOC_TYPE_8K}:
        raise ResearchValidationError("仅支持已归档的 SEC 10-K、10-Q 和 8-K。")
    url = _verified_url(document)
    client = client or _default_sec_client(dossier.security)
    raw = client.get_document_html(url, max_bytes=settings.RESEARCH_SEC_DOCUMENT_MAX_BYTES)
    text = extract_sec_html(raw)
    raw_hash = hashlib.sha256(raw).hexdigest()
    text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    now = timezone.now()
    with transaction.atomic():
        document = OfficialResearchDocument.objects.select_for_update().get(pk=document.pk)
        latest = document.content_versions.first()
        if latest and latest.raw_sha256 == raw_hash:
            document.fetched_at = now
            document.save(update_fields=["fetched_at", "updated_at"])
            return latest, False
        version = OfficialResearchContentVersion.objects.create(
            document=document, version_number=latest.version_number + 1 if latest else 1,
            source_url=url, raw_sha256=raw_hash, raw_gzip=gzip.compress(raw),
            content_text=text, content_sha256=text_hash,
            extractor_version=EXTRACTOR_VERSION, fetched_at=now,
        )
        document.content_text = text
        document.content_sha256 = text_hash
        document.fetched_at = now
        document.save(update_fields=["content_text", "content_sha256", "fetched_at", "updated_at"])
    return version, True
