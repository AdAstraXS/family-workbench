import hashlib
import html
import io
import mimetypes
import re
import tempfile
import urllib.parse
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath

import nh3
from django.core.files import File
from django.core.files.base import ContentFile
from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.html import strip_tags

from .content import content_hash, validate_resource_mime, validate_resource_signature
from .models import (
    KnowledgeAsset,
    KnowledgeDocument,
    KnowledgeImportBatch,
    KnowledgeImportItem,
    KnowledgeJob,
    KnowledgeJobItem,
    KnowledgeProposal,
    KnowledgeRevision,
    KnowledgeSource,
    KnowledgeVisibility,
)
from .search import index_document


HTML_IMPORT_CONVERTER_VERSION = "wechat-html-v1"
MAX_ARCHIVE_FILES = 10_000
MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
MAX_SINGLE_FILE_BYTES = 100 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200
IMPORT_TAGS = {
    "a",
    "b",
    "blockquote",
    "br",
    "code",
    "del",
    "div",
    "em",
    "figcaption",
    "figure",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "hr",
    "i",
    "img",
    "li",
    "mark",
    "ol",
    "p",
    "pre",
    "s",
    "section",
    "span",
    "strong",
    "sub",
    "sup",
    "table",
    "tbody",
    "td",
    "th",
    "thead",
    "tr",
    "u",
    "ul",
}
IMPORT_ATTRIBUTES = {
    "a": {"href", "title"},
    "img": {"src", "alt", "title", "width", "height"},
    "li": {"data-tag"},
    "p": {"data-tag"},
    "span": {"data-tag"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan"},
}
VOID_TAGS = {"br", "hr", "img"}
BLOCKED_CONTAINER_TAGS = {
    "applet",
    "form",
    "frameset",
    "head",
    "iframe",
    "noscript",
    "script",
    "style",
}


class KnowledgeImportError(ValueError):
    pass


class ImportJobCancelled(RuntimeError):
    pass


@dataclass(frozen=True)
class ArticleImage:
    index: int
    source_url: str
    alt: str = ""


@dataclass
class ParsedHtmlArticle:
    title: str
    author: str
    source_url: str
    published_at: datetime | None
    publisher: str
    body_template: str
    images: list[ArticleImage] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class WeChatArticleParser(HTMLParser):
    """Extract only the WeChat article body and trustworthy page metadata."""

    TEXT_TARGETS = {
        "activity-name": "heading",
        "publish_time": "published",
        "js_name": "publisher",
    }

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.metadata = {}
        self.text_values = {value: [] for value in self.TEXT_TARGETS.values()}
        self._captures = []
        self._content_depth = 0
        self._blocked_depth = 0
        self._output = []
        self.images = []
        self._global_image_index = -1

    @staticmethod
    def _attrs(attrs):
        return {str(key).lower(): value or "" for key, value in attrs}

    def _advance_captures(self):
        for capture in self._captures:
            capture[1] += 1

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        attributes = self._attrs(attrs)
        self._advance_captures()
        target = self.TEXT_TARGETS.get(attributes.get("id", ""))
        if target:
            self._captures.append([target, 1])

        if tag == "meta":
            key = (attributes.get("property") or attributes.get("name") or "").lower()
            if key and attributes.get("content") is not None:
                self.metadata.setdefault(key, attributes.get("content", "").strip())

        if tag == "img":
            self._global_image_index += 1

        if not self._content_depth and attributes.get("id") == "js_content":
            self._content_depth = 1
            return
        if not self._content_depth:
            return

        if tag in BLOCKED_CONTAINER_TAGS:
            self._blocked_depth += 1
        if self._blocked_depth:
            if tag not in VOID_TAGS:
                self._content_depth += 1
            return

        if tag == "img":
            source_url = (
                attributes.get("data-src")
                or attributes.get("data-original")
                or attributes.get("src")
                or ""
            )
            image = ArticleImage(
                index=self._global_image_index,
                source_url=source_url,
                alt=attributes.get("alt", ""),
            )
            self.images.append(image)
            clean_attrs = {
                "src": f"__KNOWLEDGE_IMPORT_IMAGE_{image.index}__",
                "alt": image.alt,
                "title": attributes.get("title", ""),
                "width": attributes.get("width", ""),
                "height": attributes.get("height", ""),
            }
        elif tag == "a":
            clean_attrs = {
                "href": attributes.get("href", ""),
                "title": attributes.get("title", ""),
            }
        else:
            clean_attrs = {
                key: value
                for key, value in attributes.items()
                if key in {"colspan", "rowspan", "data-tag"}
            }
        if tag in IMPORT_TAGS:
            rendered_attrs = "".join(
                f' {key}="{html.escape(str(value), quote=True)}"'
                for key, value in clean_attrs.items()
                if value not in (None, "")
            )
            self._output.append(f"<{tag}{rendered_attrs}>")
        if tag not in VOID_TAGS:
            self._content_depth += 1

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag.lower() not in VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        for capture in list(self._captures):
            capture[1] -= 1
            if capture[1] <= 0:
                self._captures.remove(capture)

        if not self._content_depth:
            return
        if self._blocked_depth:
            if tag in BLOCKED_CONTAINER_TAGS:
                self._blocked_depth -= 1
            self._content_depth = max(0, self._content_depth - 1)
            return
        if self._content_depth == 1:
            self._content_depth = 0
            return
        if tag in IMPORT_TAGS and tag not in VOID_TAGS:
            self._output.append(f"</{tag}>")
        self._content_depth -= 1

    def handle_data(self, data):
        for target, _depth in self._captures:
            self.text_values[target].append(data)
        if self._content_depth and not self._blocked_depth:
            self._output.append(html.escape(data))

    def handle_entityref(self, name):
        if self._content_depth and not self._blocked_depth:
            self._output.append(f"&{name};")

    def handle_charref(self, name):
        if self._content_depth and not self._blocked_depth:
            self._output.append(f"&#{name};")

    @property
    def body_template(self):
        return "".join(self._output)


def _clean_text(value):
    return re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()


def _metadata_long_text(value):
    value = html.unescape(str(value or "")).replace("\\r\\n", "\n").replace("\\n", "\n")
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in value.split("\n")).strip()


def _filename_metadata(relative_path):
    stem = Path(PurePosixPath(relative_path).name).stem
    match = re.match(r"^\[(\d{12})\](.*)$", stem)
    if not match:
        return stem, None
    title = match.group(2).strip() or stem
    try:
        naive = datetime.strptime(match.group(1), "%Y%m%d%H%M")
        published_at = timezone.make_aware(naive, timezone.get_current_timezone())
    except ValueError:
        published_at = None
    return title, published_at


def _exported_cover_stem(value):
    """Normalize the underscore added by the WeChat exporter to cover names."""
    return re.sub(r"^(\[\d{12}\])_", r"\1", str(value))


def _parse_published_at(value):
    value = _clean_text(value)
    for pattern in ("%Y年%m月%d日 %H:%M", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M"):
        try:
            naive = datetime.strptime(value, pattern)
            return timezone.make_aware(naive, timezone.get_current_timezone())
        except ValueError:
            continue
    parsed = parse_datetime(value)
    if parsed and timezone.is_naive(parsed):
        return timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


def _safe_source_url(value):
    try:
        parsed = urllib.parse.urlsplit(str(value or "").strip())
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))[:1000]


def _external_id(source_url, relative_path):
    parsed = urllib.parse.urlsplit(source_url)
    query = urllib.parse.parse_qs(parsed.query)
    if parsed.hostname == "mp.weixin.qq.com":
        biz = (query.get("__biz") or [""])[0]
        mid = (query.get("mid") or [""])[0]
        idx = (query.get("idx") or [""])[0]
        if biz and mid and idx:
            return f"wechat:{biz}:{mid}:{idx}"[:500]
    normalized_path = str(PurePosixPath(relative_path)).casefold()
    if len(normalized_path) <= 480:
        return f"path:{normalized_path}"
    digest = hashlib.sha256(normalized_path.encode("utf-8")).hexdigest()
    return f"path-sha256:{digest}"


def parse_wechat_html(raw_bytes, relative_path):
    raw_html = raw_bytes.decode("utf-8-sig", errors="replace")
    parser = WeChatArticleParser()
    parser.feed(raw_html)
    filename_title, filename_published_at = _filename_metadata(relative_path)
    metadata_title = _metadata_long_text(parser.metadata.get("og:title"))
    title = _clean_text(
        metadata_title
        or "".join(parser.text_values["heading"])
        or filename_title
    )[:500]
    author = _clean_text(
        parser.metadata.get("author")
        or parser.metadata.get("og:article:author")
    )[:300]
    source_url = _safe_source_url(parser.metadata.get("og:url"))
    published_at = _parse_published_at("".join(parser.text_values["published"]))
    published_at = published_at or filename_published_at
    publisher = _clean_text("".join(parser.text_values["publisher"]))[:300]
    warnings = []
    if not source_url:
        warnings.append("未找到可核查的原文链接，已使用相对路径作为稳定身份。")
    if not author:
        warnings.append("未找到作者。")
    if not published_at:
        warnings.append("未找到发布日期，未使用文件修改时间猜测。")
    body_template = parser.body_template
    if not body_template.strip():
        paragraphs = [part.strip() for part in re.split(r"\n\s*\n+", metadata_title) if part.strip()]
        if not paragraphs:
            raise KnowledgeImportError("未找到微信公众号正文区域 #js_content。")
        body_template = "".join(f"<p>{html.escape(part)}</p>" for part in paragraphs)
        title = _clean_text(paragraphs[0])
        if len(title) > 120:
            title = f"{title[:119]}…"
        warnings.append("未找到标准正文区，已按公众号纯文字动态从页面元数据恢复正文。")
    return ParsedHtmlArticle(
        title=title or filename_title or "未命名文章",
        author=author,
        source_url=source_url,
        published_at=published_at,
        publisher=publisher,
        body_template=body_template,
        images=parser.images,
        warnings=warnings,
    )


def _render_article(article, resource_urls):
    rendered = article.body_template
    for image in article.images:
        marker = f"__KNOWLEDGE_IMPORT_IMAGE_{image.index}__"
        url = resource_urls.get(image.index)
        if url:
            rendered = rendered.replace(marker, html.escape(url, quote=True))
        else:
            rendered = re.sub(
                rf"<img\b[^>]*src=[\"']{re.escape(marker)}[\"'][^>]*>",
                "",
                rendered,
                flags=re.IGNORECASE,
            )
    safe_html = nh3.clean(
        rendered,
        tags=IMPORT_TAGS,
        attributes=IMPORT_ATTRIBUTES,
        url_schemes={"http", "https"},
        strip_comments=True,
        link_rel="nofollow noopener noreferrer",
    )
    plain_text = html.unescape(strip_tags(safe_html))
    plain_text = re.sub(r"[ \t\f\v]+", " ", plain_text)
    plain_text = re.sub(r"\n\s*\n\s*\n+", "\n\n", plain_text).strip()
    return safe_html, plain_text


def _normalized_path(value):
    value = urllib.parse.unquote(str(value or "")).replace("\\", "/")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise KnowledgeImportError(f"导入包包含不安全路径：{value}")
    return str(path)


def _validate_zip(info_list):
    if len(info_list) > MAX_ARCHIVE_FILES:
        raise KnowledgeImportError(f"导入包文件数量超过 {MAX_ARCHIVE_FILES} 个。")
    total_bytes = 0
    names = set()
    for info in info_list:
        path = _normalized_path(info.filename)
        if path in names:
            raise KnowledgeImportError(f"导入包包含重复路径：{path}")
        names.add(path)
        if info.is_dir():
            continue
        if info.file_size > MAX_SINGLE_FILE_BYTES:
            raise KnowledgeImportError(f"单个文件超过 100 MB：{path}")
        total_bytes += info.file_size
        if total_bytes > MAX_ARCHIVE_BYTES:
            raise KnowledgeImportError("导入包展开后超过 2 GB。")
        if (
            info.file_size > 1024 * 1024
            and info.file_size / max(info.compress_size, 1) > MAX_COMPRESSION_RATIO
        ):
            raise KnowledgeImportError(f"文件压缩倍率异常：{path}")
    return total_bytes


@contextmanager
def _open_batch_package(batch):
    with batch.package_file.open("rb") as handle:
        if zipfile.is_zipfile(handle):
            handle.seek(0)
            with zipfile.ZipFile(handle) as archive:
                info_list = archive.infolist()
                _validate_zip(info_list)
                names = {
                    _normalized_path(info.filename): info
                    for info in info_list
                    if not info.is_dir()
                }
                yield archive, names
        else:
            handle.seek(0)
            raw = handle.read(MAX_SINGLE_FILE_BYTES + 1)
            if len(raw) > MAX_SINGLE_FILE_BYTES:
                raise KnowledgeImportError("单个 HTML 文件超过 100 MB。")
            name = _normalized_path(batch.source_filename)
            yield None, {name: raw}


def _read_package_file(archive, names, path):
    value = names[path]
    return archive.read(value) if archive else value


def _image_mime(path, body):
    mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
    mime = validate_resource_mime(mime, True, body)
    validate_resource_signature(body, mime)
    return mime


def _asset_map(names, html_path, article):
    html_parent = PurePosixPath(html_path).parent
    archive_paths = set(names)
    mapped = {}
    warnings = []
    for image in article.images:
        parsed = urllib.parse.urlsplit(image.source_url)
        if not parsed.scheme and image.source_url:
            try:
                candidate = _normalized_path(str(html_parent / urllib.parse.unquote(parsed.path)))
            except KnowledgeImportError:
                candidate = ""
            if candidate in archive_paths:
                mapped[image.index] = candidate

    title_candidates = {
        article.title,
        _filename_metadata(html_path)[0],
    }
    ordinal_assets = {}
    for path in archive_paths:
        pure = PurePosixPath(path)
        if len(pure.parts) < 3 or pure.parts[-3] != "图片":
            continue
        if pure.parts[-2] not in title_candidates:
            continue
        match = re.search(r"_(\d+)\.[^.]+$", pure.name)
        if match:
            ordinal_assets[int(match.group(1))] = path
    for image in article.images:
        if image.index not in mapped and image.index in ordinal_assets:
            mapped[image.index] = ordinal_assets[image.index]
    for image in article.images:
        if image.index not in mapped:
            warnings.append(f"正文图片 #{image.index} 未找到对应的本地文件，预览中不会加载远程图片。")

    stem = Path(PurePosixPath(html_path).name).stem
    cover = next(
        (
            path
            for path in archive_paths
            if PurePosixPath(path).parent.name == "封面"
            and _exported_cover_stem(Path(PurePosixPath(path).name).stem) == stem
        ),
        "",
    )
    assets = [
        {"index": index, "path": path, "role": "body"}
        for index, path in sorted(mapped.items())
    ]
    if cover and cover not in mapped.values():
        assets.append({"index": None, "path": cover, "role": "cover"})
    return assets, warnings


def _job_cancelled(job):
    current = KnowledgeJob.objects.filter(pk=job.pk).values_list("status", flat=True).first()
    if current == KnowledgeJob.STATUS_CANCEL_REQUESTED:
        raise ImportJobCancelled("任务已按成员请求取消。")


def _job_progress(job, counters):
    KnowledgeJob.objects.filter(pk=job.pk).update(
        heartbeat_at=timezone.now(),
        **counters,
    )


def _job_item(job, item, status, error_message="", details=None):
    return KnowledgeJobItem.objects.update_or_create(
        job=job,
        external_id=f"import-item-{item.pk}",
        defaults={
            "title": item.title[:500],
            "status": status,
            "error_message": str(error_message)[:4000],
            "details": details or {"relative_path": item.relative_path},
        },
    )[0]


def preview_import_batch(job):
    batch = KnowledgeImportBatch.objects.select_related("source", "requested_by").get(
        pk=job.parameters.get("batch_id")
    )
    if batch.status in {
        KnowledgeImportBatch.STATUS_IMPORTING,
        KnowledgeImportBatch.STATUS_COMPLETED,
        KnowledgeImportBatch.STATUS_ROLLING_BACK,
    }:
        raise KnowledgeImportError("该批次已经进入执行阶段，不能重新生成预览。")
    if batch.import_format != KnowledgeImportBatch.FORMAT_HTML:
        raise KnowledgeImportError("当前试导入只开放 HTML；Markdown 将在取得真实样本后接入。")
    batch.status = KnowledgeImportBatch.STATUS_PREVIEWING
    batch.error_message = ""
    batch.save(update_fields=["status", "error_message", "updated_at"])
    batch.items.all().delete()
    counters = {"success_count": 0, "updated_count": 0, "skipped_count": 0, "failed_count": 0}
    action_counts = {
        KnowledgeImportItem.ACTION_NEW: 0,
        KnowledgeImportItem.ACTION_UPDATE: 0,
        KnowledgeImportItem.ACTION_UNCHANGED: 0,
        KnowledgeImportItem.ACTION_DUPLICATE: 0,
        KnowledgeImportItem.ACTION_ERROR: 0,
    }
    asset_count = 0
    estimated_bytes = 0
    with _open_batch_package(batch) as (archive, names):
        html_paths = sorted(path for path in names if path.lower().endswith(".html"))
        if not html_paths:
            raise KnowledgeImportError("导入包中没有 HTML 文件。")
        job.total_count = len(html_paths)
        job.save(update_fields=["total_count", "updated_at"])
        for relative_path in html_paths:
            _job_cancelled(job)
            item = KnowledgeImportItem.objects.create(
                batch=batch,
                relative_path=relative_path,
                directory_path=(
                    ""
                    if PurePosixPath(relative_path).parent == PurePosixPath(".")
                    else str(PurePosixPath(relative_path).parent)
                ),
            )
            try:
                raw = _read_package_file(archive, names, relative_path)
                article = parse_wechat_html(raw, relative_path)
                source_author = article.author
                if not article.author:
                    article.author = batch.source.name.split("·")[-1].strip()[:300]
                    article.warnings.append("页面未标注作者，已采用本批次公众号来源名称。")
                if batch.person_name:
                    article.author = batch.person_name
                safe_html, plain_text = _render_article(article, {})
                assets, asset_warnings = _asset_map(names, relative_path, article)
                asset_hashes = []
                asset_bytes = 0
                valid_assets = []
                for asset in assets:
                    body = _read_package_file(archive, names, asset["path"])
                    _image_mime(asset["path"], body)
                    digest = content_hash(body)
                    asset_hashes.append(digest)
                    asset_bytes += len(body)
                    valid_assets.append({**asset, "sha256": digest, "bytes": len(body)})
                normalized_sha = content_hash(
                    (plain_text + "\n" + "\n".join(sorted(asset_hashes))).encode("utf-8")
                )
                external_id = _external_id(article.source_url, relative_path)
                document = batch.source.documents.filter(external_id=external_id).select_related(
                    "current_revision"
                ).first()
                if document and document.current_revision_id:
                    action = (
                        KnowledgeImportItem.ACTION_UNCHANGED
                        if document.current_revision.normalized_hash == normalized_sha
                        else KnowledgeImportItem.ACTION_UPDATE
                    )
                else:
                    duplicate = (
                        KnowledgeRevision.objects.filter(
                            document__family=batch.family,
                            normalized_hash=normalized_sha,
                        )
                        .exclude(document=document)
                        .select_related("document")
                        .first()
                    )
                    action = (
                        KnowledgeImportItem.ACTION_DUPLICATE
                        if duplicate
                        else KnowledgeImportItem.ACTION_NEW
                    )
                item.external_id = external_id
                item.title = article.title
                item.author = article.author
                item.source_url = article.source_url
                item.published_at = article.published_at
                item.raw_sha256 = content_hash(raw)
                item.normalized_sha256 = normalized_sha
                item.action = action
                item.asset_count = len(valid_assets)
                item.asset_bytes = asset_bytes
                item.warnings = [*article.warnings, *asset_warnings]
                item.details = {
                    "publisher": article.publisher,
                    "source_author": source_author,
                    "plain_text_length": len(plain_text),
                    "assets": valid_assets,
                    "safe_html_length": len(safe_html),
                }
                item.document = document
                item.save()
                action_counts[action] += 1
                asset_count += len(valid_assets)
                estimated_bytes += len(raw) + asset_bytes
                if action == KnowledgeImportItem.ACTION_NEW:
                    counters["success_count"] += 1
                    status = KnowledgeJobItem.STATUS_SUCCESS
                elif action == KnowledgeImportItem.ACTION_UPDATE:
                    counters["updated_count"] += 1
                    status = KnowledgeJobItem.STATUS_UPDATED
                else:
                    counters["skipped_count"] += 1
                    status = KnowledgeJobItem.STATUS_SKIPPED
                _job_item(job, item, status, details={"action": action, "relative_path": relative_path})
            except Exception as exc:
                item.action = KnowledgeImportItem.ACTION_ERROR
                item.status = KnowledgeImportItem.STATUS_FAILED
                item.error_message = str(exc)[:4000]
                item.save(update_fields=["action", "status", "error_message", "updated_at"])
                action_counts[KnowledgeImportItem.ACTION_ERROR] += 1
                counters["failed_count"] += 1
                _job_item(job, item, KnowledgeJobItem.STATUS_FAILED, str(exc))
            _job_progress(job, counters)

    batch.total_count = sum(action_counts.values())
    batch.new_count = action_counts[KnowledgeImportItem.ACTION_NEW]
    batch.update_count = action_counts[KnowledgeImportItem.ACTION_UPDATE]
    batch.skipped_count = action_counts[KnowledgeImportItem.ACTION_UNCHANGED]
    batch.duplicate_count = action_counts[KnowledgeImportItem.ACTION_DUPLICATE]
    batch.error_count = action_counts[KnowledgeImportItem.ACTION_ERROR]
    batch.asset_count = asset_count
    batch.estimated_bytes = estimated_bytes
    batch.previewed_at = timezone.now()
    batch.status = KnowledgeImportBatch.STATUS_PREVIEW_READY
    batch.result = {"preview_actions": action_counts}
    batch.save()
    return counters


def _snapshot_document(document):
    return {
        "title": document.title,
        "author": document.author,
        "section_name": document.section_name,
        "hierarchy": document.hierarchy,
        "source_url": document.source_url,
        "visibility": document.visibility,
        "sync_status": document.sync_status,
        "curation_status": document.curation_status,
        "knowledge_status": document.knowledge_status,
        "library_tier": document.library_tier,
        "content_created_at": document.content_created_at.isoformat()
        if document.content_created_at
        else None,
        "content_modified_at": document.content_modified_at.isoformat()
        if document.content_modified_at
        else None,
        "category": document.category,
    }


def _restore_document(document, state):
    for field in (
        "title",
        "author",
        "section_name",
        "hierarchy",
        "source_url",
        "visibility",
        "sync_status",
        "curation_status",
        "knowledge_status",
        "library_tier",
        "category",
    ):
        setattr(document, field, state.get(field, getattr(document, field)))
    document.content_created_at = parse_datetime(state.get("content_created_at") or "")
    document.content_modified_at = parse_datetime(state.get("content_modified_at") or "")


def _create_import_revision(batch, item, archive, names):
    raw = _read_package_file(archive, names, item.relative_path)
    if content_hash(raw) != item.raw_sha256:
        raise KnowledgeImportError("导入包在预览后发生变化，请重新生成预览。")
    article = parse_wechat_html(raw, item.relative_path)
    assets = item.details.get("assets") or []
    created_files = []
    document = item.document
    created_document = False
    previous_state = {}
    previous_revision = None
    try:
        with transaction.atomic():
            if document is None:
                document = KnowledgeDocument.objects.create(
                    family=batch.family,
                    source=batch.source,
                    owner=batch.requested_by,
                    external_id=item.external_id,
                    title=item.title,
                    author=item.author,
                    section_name=item.directory_path or "根目录",
                    hierarchy={
                        "relative_path": item.relative_path,
                        "directory_path": item.directory_path,
                    },
                    source_url=item.source_url,
                    visibility=batch.visibility,
                    sync_status=KnowledgeDocument.SYNC_AVAILABLE,
                    curation_status=KnowledgeDocument.CURATION_NORMALIZED,
                    knowledge_status=KnowledgeDocument.KNOWLEDGE_INCLUDED,
                    library_tier=KnowledgeDocument.LIBRARY_ARCHIVE,
                    content_created_at=item.published_at,
                    content_modified_at=item.published_at,
                    category=batch.category,
                )
                created_document = True
            else:
                document = KnowledgeDocument.objects.select_for_update().select_related(
                    "current_revision"
                ).get(pk=document.pk)
                previous_state = _snapshot_document(document)
                previous_revision = document.current_revision
                document.title = item.title
                document.author = item.author
                document.section_name = item.directory_path or "根目录"
                document.hierarchy = {
                    "relative_path": item.relative_path,
                    "directory_path": item.directory_path,
                }
                document.source_url = item.source_url
                document.content_created_at = item.published_at
                document.content_modified_at = item.published_at
                document.sync_status = KnowledgeDocument.SYNC_AVAILABLE
                document.source_deleted_at = None
                if document.curation_status != KnowledgeDocument.CURATION_CONFIRMED:
                    document.curation_status = KnowledgeDocument.CURATION_NORMALIZED

            next_number = (
                document.revisions.order_by("-revision_number")
                .values_list("revision_number", flat=True)
                .first()
                or 0
            ) + 1
            revision = KnowledgeRevision.objects.create(
                document=document,
                revision_number=next_number,
                content_hash=item.raw_sha256,
                normalized_hash=item.normalized_sha256,
                raw_file="",
                normalized_html="",
                plain_text="",
                converter_version=HTML_IMPORT_CONVERTER_VERSION,
                source_modified_at=item.published_at,
            )
            revision.raw_file.save(
                PurePosixPath(item.relative_path).name,
                ContentFile(raw),
                save=True,
            )
            created_files.append((revision.raw_file.storage, revision.raw_file.name))
            resource_urls = {}
            for asset_info in assets:
                body = _read_package_file(archive, names, asset_info["path"])
                mime = _image_mime(asset_info["path"], body)
                asset = KnowledgeAsset.objects.create(
                    revision=revision,
                    external_id=(
                        f"html-image-{asset_info['index']}"
                        if asset_info.get("index") is not None
                        else "html-cover"
                    ),
                    original_name=PurePosixPath(asset_info["path"]).name,
                    source_path=asset_info["path"],
                    mime_type=mime,
                    byte_size=len(body),
                    content_hash=content_hash(body),
                    is_image=True,
                    file="",
                )
                asset.file.save(asset.original_name, ContentFile(body), save=True)
                created_files.append((asset.file.storage, asset.file.name))
                if asset_info.get("index") is not None:
                    resource_urls[asset_info["index"]] = reverse(
                        "knowledge:asset_download", kwargs={"pk": asset.pk}
                    )
            safe_html, plain_text = _render_article(article, resource_urls)
            actual_normalized_hash = content_hash(
                (
                    plain_text
                    + "\n"
                    + "\n".join(sorted(asset["sha256"] for asset in assets))
                ).encode("utf-8")
            )
            if actual_normalized_hash != item.normalized_sha256:
                raise KnowledgeImportError("规范正文与预览不一致，请重新生成预览。")
            revision.normalized_html = safe_html
            revision.plain_text = plain_text
            revision.save(update_fields=["normalized_html", "plain_text"])
            KnowledgeProposal.objects.filter(
                document=document,
                status=KnowledgeProposal.STATUS_PENDING,
            ).exclude(revision=revision).update(status=KnowledgeProposal.STATUS_STALE)
            document.current_revision = revision
            document.save()
            item.document = document
            item.revision = revision
            item.previous_revision = previous_revision
            item.previous_state = previous_state
            item.status = KnowledgeImportItem.STATUS_IMPORTED
            details = dict(item.details or {})
            details["created_document"] = created_document
            details["document_updated_at"] = document.updated_at.isoformat()
            item.details = details
            item.error_message = ""
            item.save()
            index_document(document)
        return document, revision
    except Exception:
        for storage, name in created_files:
            try:
                storage.delete(name)
            except OSError:
                pass
        raise


def import_knowledge_batch(job):
    batch = KnowledgeImportBatch.objects.select_related("source", "requested_by", "family").get(
        pk=job.parameters.get("batch_id")
    )
    if batch.status not in {
        KnowledgeImportBatch.STATUS_PREVIEW_READY,
        KnowledgeImportBatch.STATUS_PARTIAL,
    } or (batch.status == KnowledgeImportBatch.STATUS_PARTIAL and batch.rolled_back_at):
        raise KnowledgeImportError("只有完成格式检查并等待确认的批次可以导入。")
    batch.status = KnowledgeImportBatch.STATUS_IMPORTING
    batch.confirmed_at = batch.confirmed_at or timezone.now()
    batch.error_message = ""
    batch.save(update_fields=["status", "confirmed_at", "error_message", "updated_at"])
    items = list(batch.items.order_by("id"))
    job.total_count = len(items)
    job.save(update_fields=["total_count", "updated_at"])
    counters = {"success_count": 0, "updated_count": 0, "skipped_count": 0, "failed_count": 0}
    with _open_batch_package(batch) as (archive, names):
        for item in items:
            _job_cancelled(job)
            if item.status in {
                KnowledgeImportItem.STATUS_IMPORTED,
                KnowledgeImportItem.STATUS_SKIPPED,
            }:
                counters["skipped_count"] += 1
                _job_item(job, item, KnowledgeJobItem.STATUS_SKIPPED)
                _job_progress(job, counters)
                continue
            if item.action in {
                KnowledgeImportItem.ACTION_UNCHANGED,
                KnowledgeImportItem.ACTION_DUPLICATE,
            }:
                item.status = KnowledgeImportItem.STATUS_SKIPPED
                item.save(update_fields=["status", "updated_at"])
                counters["skipped_count"] += 1
                _job_item(job, item, KnowledgeJobItem.STATUS_SKIPPED)
            elif item.action == KnowledgeImportItem.ACTION_ERROR:
                counters["failed_count"] += 1
                _job_item(job, item, KnowledgeJobItem.STATUS_FAILED, item.error_message)
            else:
                try:
                    _create_import_revision(batch, item, archive, names)
                    if item.action == KnowledgeImportItem.ACTION_NEW:
                        counters["success_count"] += 1
                        status = KnowledgeJobItem.STATUS_SUCCESS
                    else:
                        counters["updated_count"] += 1
                        status = KnowledgeJobItem.STATUS_UPDATED
                    _job_item(job, item, status)
                except Exception as exc:
                    item.status = KnowledgeImportItem.STATUS_FAILED
                    item.error_message = str(exc)[:4000]
                    item.save(update_fields=["status", "error_message", "updated_at"])
                    counters["failed_count"] += 1
                    _job_item(job, item, KnowledgeJobItem.STATUS_FAILED, str(exc))
            _job_progress(job, counters)
    batch.completed_at = timezone.now()
    batch.status = (
        KnowledgeImportBatch.STATUS_PARTIAL
        if counters["failed_count"]
        else KnowledgeImportBatch.STATUS_COMPLETED
    )
    batch.result = {**(batch.result or {}), "import_counts": counters}
    batch.save(update_fields=["status", "completed_at", "result", "updated_at"])
    batch.source.last_sync_at = batch.completed_at
    batch.source.status = (
        KnowledgeSource.STATUS_ERROR
        if counters["failed_count"]
        else KnowledgeSource.STATUS_ACTIVE
    )
    batch.source.last_error = (
        f"批次 #{batch.pk} 有 {counters['failed_count']} 篇导入失败。"
        if counters["failed_count"]
        else ""
    )
    batch.source.save(update_fields=["last_sync_at", "status", "last_error", "updated_at"])
    return counters


@transaction.atomic
def assign_import_batch_person(batch, person_name):
    person_name = str(person_name or "").strip()[:300]
    if not person_name:
        raise KnowledgeImportError("归属人物不能为空。")
    if batch.status in {
        KnowledgeImportBatch.STATUS_ROLLING_BACK,
        KnowledgeImportBatch.STATUS_ROLLED_BACK,
    }:
        raise KnowledgeImportError("正在回滚或已经回滚的批次不能修改归属人物。")

    batch = KnowledgeImportBatch.objects.select_for_update().select_related("family").get(
        pk=batch.pk
    )
    items = list(
        batch.items.select_for_update()
        .order_by("id")
    )
    assigned_at = timezone.now()
    source_aliases = sorted({item.author for item in items if item.author})
    updated_documents = 0

    for item in items:
        details = dict(item.details or {})
        details.setdefault("source_author", item.author)
        if item.status == KnowledgeImportItem.STATUS_IMPORTED and item.document_id:
            document = KnowledgeDocument.objects.select_for_update().get(pk=item.document_id)
            expected_updated_at = details.get("document_updated_at")
            if (
                document.current_revision_id != item.revision_id
                or not expected_updated_at
                or document.updated_at.isoformat() != expected_updated_at
            ):
                raise KnowledgeImportError(
                    f"《{item.title or item.relative_path}》在导入后已有修改，已停止人物归并。"
                )
            document.author = person_name
            document.save(update_fields=["author", "updated_at"])
            index_document(document)
            details["document_updated_at"] = document.updated_at.isoformat()
            updated_documents += 1

        details["person_assignment"] = {
            "name": person_name,
            "assigned_at": assigned_at.isoformat(),
        }
        item.author = person_name
        item.details = details
        item.save(update_fields=["author", "details", "updated_at"])

    result = dict(batch.result or {})
    result["person_assignment"] = {
        "name": person_name,
        "source_aliases": source_aliases,
        "document_count": updated_documents,
        "assigned_at": assigned_at.isoformat(),
    }
    batch.person_name = person_name
    batch.result = result
    batch.save(update_fields=["person_name", "result", "updated_at"])
    return {
        "person_name": person_name,
        "source_aliases": source_aliases,
        "item_count": len(items),
        "document_count": updated_documents,
    }


def _revision_file_records(revision):
    records = []
    if revision.raw_file:
        records.append((revision.raw_file.storage, revision.raw_file.name))
    for asset in revision.assets.all():
        if asset.file:
            records.append((asset.file.storage, asset.file.name))
    return records


def rollback_knowledge_batch(job):
    batch = KnowledgeImportBatch.objects.select_related("source").get(
        pk=job.parameters.get("batch_id")
    )
    if batch.status not in {
        KnowledgeImportBatch.STATUS_COMPLETED,
        KnowledgeImportBatch.STATUS_PARTIAL,
    }:
        raise KnowledgeImportError("只有已执行的导入批次可以回滚。")
    batch.status = KnowledgeImportBatch.STATUS_ROLLING_BACK
    batch.error_message = ""
    batch.save(update_fields=["status", "error_message", "updated_at"])
    items = list(batch.items.select_related("document", "revision", "previous_revision").order_by("-id"))
    job.total_count = len(items)
    job.save(update_fields=["total_count", "updated_at"])
    counters = {"success_count": 0, "updated_count": 0, "skipped_count": 0, "failed_count": 0}
    for item in items:
        _job_cancelled(job)
        if item.status != KnowledgeImportItem.STATUS_IMPORTED or not item.document_id or not item.revision_id:
            item.status = KnowledgeImportItem.STATUS_ROLLED_BACK
            item.save(update_fields=["status", "updated_at"])
            counters["skipped_count"] += 1
            _job_item(job, item, KnowledgeJobItem.STATUS_SKIPPED)
            _job_progress(job, counters)
            continue
        document = KnowledgeDocument.objects.select_related("current_revision").get(pk=item.document_id)
        expected_updated_at = (item.details or {}).get("document_updated_at")
        if (
            document.current_revision_id != item.revision_id
            or not expected_updated_at
            or document.updated_at.isoformat() != expected_updated_at
        ):
            message = "文档在该批次后已有修改，已阻止自动回滚。"
            item.error_message = message
            item.save(update_fields=["error_message", "updated_at"])
            counters["failed_count"] += 1
            _job_item(job, item, KnowledgeJobItem.STATUS_FAILED, message)
            _job_progress(job, counters)
            continue
        files = _revision_file_records(item.revision)
        try:
            with transaction.atomic():
                if (item.details or {}).get("created_document"):
                    if document.revisions.count() != 1:
                        raise KnowledgeImportError("新建文档已有其他版本，已阻止自动回滚。")
                    document.delete()
                else:
                    document.current_revision = item.previous_revision
                    _restore_document(document, item.previous_state or {})
                    document.save()
                    item.revision.delete()
                    index_document(document)
                item.document = document if document.pk else None
                item.revision = None
                item.status = KnowledgeImportItem.STATUS_ROLLED_BACK
                item.error_message = ""
                item.save(update_fields=["document", "revision", "status", "error_message", "updated_at"])
            for storage, name in files:
                try:
                    storage.delete(name)
                except OSError:
                    pass
            counters["success_count"] += 1
            _job_item(job, item, KnowledgeJobItem.STATUS_SUCCESS)
        except Exception as exc:
            item.error_message = str(exc)[:4000]
            item.save(update_fields=["error_message", "updated_at"])
            counters["failed_count"] += 1
            _job_item(job, item, KnowledgeJobItem.STATUS_FAILED, str(exc))
        _job_progress(job, counters)
    batch.rolled_back_at = timezone.now()
    batch.status = (
        KnowledgeImportBatch.STATUS_PARTIAL
        if counters["failed_count"]
        else KnowledgeImportBatch.STATUS_ROLLED_BACK
    )
    batch.result = {**(batch.result or {}), "rollback_counts": counters}
    batch.save(update_fields=["status", "rolled_back_at", "result", "updated_at"])
    return counters


def _matching_sample_assets(root, html_path):
    title, _published = _filename_metadata(html_path.name)
    candidates = []
    image_dir = root / "图片" / title
    if image_dir.is_dir():
        candidates.extend(path for path in image_dir.rglob("*") if path.is_file())
    cover_dir = root / "封面"
    if cover_dir.is_dir():
        candidates.extend(
            path
            for path in cover_dir.iterdir()
            if path.is_file()
            and _exported_cover_stem(path.stem) == html_path.stem
        )
    return candidates


def _resolved_person_name(source, person_name):
    person_name = str(person_name or "").strip()[:300]
    if person_name:
        return person_name
    return (
        source.import_batches.exclude(person_name="")
        .order_by("-created_at")
        .values_list("person_name", flat=True)
        .first()
        or ""
    )


def stage_html_directory_batch(
    *,
    root,
    html_files,
    family,
    member,
    source_name,
    source_key,
    person_name="",
    visibility=KnowledgeVisibility.FAMILY,
    category="公众号归档",
):
    root = Path(root).resolve()
    selected = []
    for value in html_files:
        path = Path(value)
        if not path.is_absolute():
            path = root / path
        path = path.resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise KnowledgeImportError("样本文件必须位于指定根目录内。") from exc
        if path.suffix.lower() != ".html" or not path.is_file():
            raise KnowledgeImportError(f"不是可用的 HTML 文件：{path.name}")
        selected.append(path)
    if not selected:
        raise KnowledgeImportError("至少选择一个 HTML 文件。")

    source, _ = KnowledgeSource.objects.get_or_create(
        family=family,
        key=source_key,
        defaults={
            "owner": member,
            "kind": KnowledgeSource.KIND_HTML_IMPORT,
            "name": source_name,
            "visibility": visibility,
            "allow_cloud_ai": False,
            "status": KnowledgeSource.STATUS_ACTIVE,
        },
    )
    person_name = _resolved_person_name(source, person_name)
    spool = tempfile.SpooledTemporaryFile(max_size=20 * 1024 * 1024, mode="w+b")
    with zipfile.ZipFile(spool, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        added = set()
        for html_path in selected:
            paths = [html_path, *_matching_sample_assets(root, html_path)]
            for path in paths:
                relative = path.relative_to(root).as_posix()
                if relative not in added:
                    archive.write(path, relative)
                    added.add(relative)
    spool.seek(0)
    digest = hashlib.sha256()
    while chunk := spool.read(1024 * 1024):
        digest.update(chunk)
    spool.seek(0)
    batch = KnowledgeImportBatch.objects.create(
        family=family,
        source=source,
        requested_by=member,
        import_format=KnowledgeImportBatch.FORMAT_HTML,
        source_filename="wechat-html-sample.zip",
        source_sha256=digest.hexdigest(),
        package_file="",
        visibility=visibility,
        person_name=person_name,
        category=category,
    )
    batch.package_file.save(
        batch.source_filename,
        File(spool),
        save=True,
    )
    return batch


def create_uploaded_import_batch(
    *, member, source_name, category, visibility, uploaded_file, person_name=""
):
    filename = Path(getattr(uploaded_file, "name", "knowledge-import.zip")).name
    suffix = Path(filename).suffix.lower()
    if suffix not in {".zip", ".html"}:
        raise KnowledgeImportError("目前网页上传仅支持 .zip 或单个 .html 文件。")
    digest = hashlib.sha256()
    size = 0
    for chunk in uploaded_file.chunks():
        size += len(chunk)
        if size > MAX_ARCHIVE_BYTES:
            raise KnowledgeImportError("上传文件超过 2 GB。")
        digest.update(chunk)
    uploaded_file.seek(0)
    source_key = f"html-import:{member.pk}:{hashlib.sha256(source_name.encode('utf-8')).hexdigest()[:20]}"
    source, _ = KnowledgeSource.objects.get_or_create(
        family=member.family,
        key=source_key,
        defaults={
            "owner": member,
            "kind": KnowledgeSource.KIND_HTML_IMPORT,
            "name": source_name,
            "visibility": visibility,
            "allow_cloud_ai": False,
        },
    )
    person_name = _resolved_person_name(source, person_name)
    batch = KnowledgeImportBatch.objects.create(
        family=member.family,
        source=source,
        requested_by=member,
        import_format=KnowledgeImportBatch.FORMAT_HTML,
        source_filename=filename,
        source_sha256=digest.hexdigest(),
        package_file="",
        visibility=visibility,
        person_name=person_name,
        category=category,
    )
    batch.package_file.save(filename, uploaded_file, save=True)
    return batch
