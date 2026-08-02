import hashlib
import html
import mimetypes
import re
import urllib.parse
from dataclasses import dataclass, field
from html.parser import HTMLParser

import nh3
from django.utils.html import strip_tags


SAFE_IMAGE_MIME_TYPES = {
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/webp",
}
DANGEROUS_MIME_TYPES = {
    "application/ecmascript",
    "application/javascript",
    "application/x-executable",
    "application/x-msdownload",
    "application/x-sh",
    "image/svg+xml",
    "text/html",
    "text/javascript",
}
ALLOWED_TAGS = {
    "a",
    "b",
    "blockquote",
    "br",
    "code",
    "div",
    "em",
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
    "ol",
    "p",
    "pre",
    "s",
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
ALLOWED_ATTRIBUTES = {
    "a": {"href", "title"},
    "img": {"src", "alt", "title", "width", "height"},
    "li": {"data-tag"},
    "p": {"data-tag"},
    "span": {"data-tag"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan"},
}


class UnsafeKnowledgeResourceError(ValueError):
    pass


def content_hash(value):
    return hashlib.sha256(value).hexdigest()


def resource_external_id(url):
    parsed = urllib.parse.urlsplit(url)
    match = re.search(r"/resources/([^/$?]+)/", parsed.path, re.IGNORECASE)
    if match:
        return urllib.parse.unquote(match.group(1))[:500]
    return content_hash(url.encode("utf-8"))


@dataclass
class ResourceReference:
    url: str
    aliases: set[str] = field(default_factory=set)
    is_image: bool = False
    original_name: str = ""
    declared_mime: str = ""


class ResourceReferenceParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.references = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag.lower() == "img":
            primary = attrs.get("data-fullres-src") or attrs.get("src")
            if not primary:
                return
            aliases = {
                value
                for value in (attrs.get("src"), attrs.get("data-fullres-src"))
                if value
            }
            self.references.append(
                ResourceReference(
                    url=primary,
                    aliases=aliases,
                    is_image=True,
                    original_name=attrs.get("alt", ""),
                    declared_mime=(
                        attrs.get("data-fullres-src-type")
                        or attrs.get("data-src-type")
                        or ""
                    ),
                )
            )
        elif tag.lower() == "object" and attrs.get("data"):
            self.references.append(
                ResourceReference(
                    url=attrs["data"],
                    aliases={attrs["data"]},
                    is_image=False,
                    original_name=attrs.get("data-attachment", ""),
                    declared_mime=attrs.get("type", ""),
                )
            )


def extract_resource_references(raw_html):
    parser = ResourceReferenceParser()
    parser.feed(raw_html)
    unique = {}
    for reference in parser.references:
        if not is_allowed_microsoft_resource_url(reference.url):
            continue
        current = unique.get(reference.url)
        if current:
            current.aliases.update(reference.aliases)
            continue
        reference.aliases = {
            alias
            for alias in reference.aliases
            if is_allowed_microsoft_resource_url(alias)
        }
        unique[reference.url] = reference
    return list(unique.values())


def is_allowed_microsoft_resource_url(value):
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname in {"graph.microsoft.com", "www.onenote.com", "onenote.com"}
        and "/resources/" in parsed.path.lower()
        and not parsed.username
        and not parsed.password
    )


def detect_safe_image_mime(body):
    if body.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if body.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if body.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if body.startswith(b"RIFF") and body[8:12] == b"WEBP":
        return "image/webp"
    return ""


def validate_resource_mime(mime_type, is_image, body=b""):
    normalized = (mime_type or "application/octet-stream").split(";", 1)[0].lower()
    if normalized in DANGEROUS_MIME_TYPES:
        raise UnsafeKnowledgeResourceError(f"不允许保存危险文件类型：{normalized}")
    if is_image and normalized == "application/octet-stream":
        normalized = detect_safe_image_mime(body)
        if not normalized:
            raise UnsafeKnowledgeResourceError(
                "正文图片缺少可信类型，且文件内容无法识别为受支持的图片。"
            )
    if is_image and normalized not in SAFE_IMAGE_MIME_TYPES:
        raise UnsafeKnowledgeResourceError(f"不支持的正文图片类型：{normalized}")
    return normalized


def validate_resource_signature(body, mime_type):
    signatures = {
        "image/gif": (b"GIF87a", b"GIF89a"),
        "image/jpeg": (b"\xff\xd8\xff",),
        "image/png": (b"\x89PNG\r\n\x1a\n",),
        "image/webp": (b"RIFF",),
        "application/pdf": (b"%PDF-",),
    }
    expected = signatures.get(mime_type)
    if expected and not any(body.startswith(signature) for signature in expected):
        raise UnsafeKnowledgeResourceError(
            f"文件内容与声明类型 {mime_type} 不一致。"
        )
    if mime_type == "image/webp" and body[8:12] != b"WEBP":
        raise UnsafeKnowledgeResourceError("文件内容不是有效的 WebP 图片。")
    if body.startswith((b"MZ", b"\x7fELF", b"#!")):
        raise UnsafeKnowledgeResourceError("不允许保存可执行文件或脚本附件。")


def suggested_filename(reference, mime_type):
    name = (reference.original_name or "").strip()
    if name:
        return name[:250]
    extension = mimetypes.guess_extension(mime_type) or ".bin"
    return f"resource-{resource_external_id(reference.url)[:24]}{extension}"


class OneNoteHTMLRewriter(HTMLParser):
    VOID_TAGS = {"br", "hr", "img"}
    BLOCKED_TAGS = {
        "applet",
        "embed",
        "form",
        "frame",
        "frameset",
        "iframe",
        "input",
        "link",
        "meta",
        "noscript",
        "script",
        "style",
    }

    def __init__(self, resource_urls):
        super().__init__(convert_charrefs=False)
        self.resource_urls = resource_urls
        self.output = []
        self.blocked_depth = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        attrs = dict(attrs)
        if tag in self.BLOCKED_TAGS:
            self.blocked_depth += 1
            return
        if self.blocked_depth:
            return
        if tag == "object":
            resource_url = self.resource_urls.get(attrs.get("data", ""))
            if resource_url:
                name = html.escape(attrs.get("data-attachment") or "下载附件")
                self.output.append(
                    f'<a href="{html.escape(resource_url, quote=True)}">{name}</a>'
                )
            return
        if tag == "img":
            original = attrs.get("data-fullres-src") or attrs.get("src", "")
            local_url = self.resource_urls.get(original)
            if not local_url:
                return
            clean_attrs = {
                "src": local_url,
                "alt": attrs.get("alt", ""),
                "title": attrs.get("title", ""),
                "width": attrs.get("width", ""),
                "height": attrs.get("height", ""),
            }
        elif tag == "a":
            clean_attrs = {
                "href": attrs.get("href", ""),
                "title": attrs.get("title", ""),
            }
        else:
            clean_attrs = {
                key: value
                for key, value in attrs.items()
                if key in {"colspan", "rowspan", "data-tag"}
            }
        rendered_attrs = "".join(
            f' {key}="{html.escape(str(value), quote=True)}"'
            for key, value in clean_attrs.items()
            if value not in (None, "")
        )
        self.output.append(f"<{tag}{rendered_attrs}>")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in self.BLOCKED_TAGS:
            if self.blocked_depth:
                self.blocked_depth -= 1
            return
        if self.blocked_depth or tag in {"object", *self.VOID_TAGS}:
            return
        self.output.append(f"</{tag}>")

    def handle_data(self, data):
        if not self.blocked_depth:
            self.output.append(html.escape(data))

    def handle_entityref(self, name):
        if not self.blocked_depth:
            self.output.append(f"&{name};")

    def handle_charref(self, name):
        if not self.blocked_depth:
            self.output.append(f"&#{name};")


def normalize_onenote_html(raw_html, resource_urls):
    rewriter = OneNoteHTMLRewriter(resource_urls)
    rewriter.feed(raw_html)
    rewritten = "".join(rewriter.output)
    safe_html = nh3.clean(
        rewritten,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        url_schemes={"http", "https"},
        strip_comments=True,
        link_rel="nofollow noopener noreferrer",
    )
    plain_text = html.unescape(strip_tags(safe_html))
    plain_text = re.sub(r"[ \t\f\v]+", " ", plain_text)
    plain_text = re.sub(r"\n\s*\n\s*\n+", "\n\n", plain_text).strip()
    return safe_html, plain_text
