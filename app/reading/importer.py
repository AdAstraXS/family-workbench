"""Deterministic, script-free reading derivatives; original files stay immutable."""
import html
import posixpath
import re
import stat
import zipfile
import xml.etree.ElementTree as ET
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

VERSION = "safe-epub-1"
MAX_ENTRIES = 10000
MAX_EXPANDED = 512 * 1024 * 1024
MAX_ENTRY = 16 * 1024 * 1024
MAX_XML = 4 * 1024 * 1024
XHTML = "http://www.w3.org/1999/xhtml"
OPF = "http://www.idpf.org/2007/opf"
ALLOWED_TAGS = set("html head title body section article main aside nav div p span a h1 h2 h3 h4 h5 h6 blockquote pre code em strong b i u s small sub sup br hr ul ol li dl dt dd table thead tbody tfoot tr th td caption colgroup col img figure figcaption ruby rt rp abbr cite q time header footer".split())
ATTRS = {"id", "class", "title", "lang", "dir", "alt", "colspan", "rowspan", "scope", "start", "value"}
MEDIA = {"application/xhtml+xml", "text/html", "text/css", "application/x-dtbncx+xml",
         "image/jpeg", "image/png", "image/gif", "image/webp", "image/svg+xml"}


class ImportFailure(ValueError):
    pass


def local_name(tag):
    return tag.rsplit("}", 1)[-1].lower()


def parse_xml(data):
    if b"\x00" in data:
        raise ImportFailure("本阶段 XML 仅支持 UTF-8 编码，请转换后重试。")
    if len(data) > MAX_XML:
        raise ImportFailure("章节或目录超过 4 MB，暂不能解析。")
    # No entity declarations/internal DTD. Ordinary external DOCTYPE is discarded,
    # never fetched. ET also drops processing instructions.
    if re.search(br"<!\s*ENTITY", data, re.I):
        raise ImportFailure("文件含不支持的 XML 实体声明。")
    data = re.sub(br"<!DOCTYPE\s+[^<>\[\]]*>\s*", b"", data, flags=re.I)
    if re.search(br"<!\s*DOCTYPE", data, re.I):
        raise ImportFailure("文件含不支持的 DTD。")
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise ImportFailure("章节或目录 XML 损坏，不能生成稳定阅读内容。") from exc


def resolve_path(value, base, available):
    value = unquote(value)
    if any(ord(c) < 32 for c in value) or "\\" in value:
        return None
    parts = urlsplit(value)
    if parts.scheme or parts.netloc or parts.query or parts.path.startswith("/"):
        return None
    path = posixpath.normpath(posixpath.join(posixpath.dirname(base), parts.path)) if parts.path else base
    return path if path in available else None


def sanitize_xhtml(data, path, available):
    root = parse_xml(data)
    def clean(node):
        tag = local_name(node.tag)
        # SVG wrappers used for raster cover images become ordinary safe <img>.
        if tag == "image":
            ref = node.get("{http://www.w3.org/1999/xlink}href", node.get("href", ""))
            if resolve_path(ref, path, available):
                node.tag = f"{{{XHTML}}}img"
                node.attrib.clear()
                node.set("src", ref)
                return
        if tag not in ALLOWED_TAGS:
            return
        node.tag = f"{{{XHTML}}}{tag}"
        for key in list(node.attrib):
            value = node.attrib[key]
            if key in {"src", "href"}:
                if not resolve_path(value, path, available) or (key == "src" and tag != "img"):
                    del node.attrib[key]
            elif key not in ATTRS and key not in {"{http://www.idpf.org/2007/ops}type", "{http://www.w3.org/XML/1998/namespace}lang"}:
                del node.attrib[key]
        for child in list(node):
            child_tag = local_name(child.tag)
            if child_tag == "svg":
                replacements = [e for e in child.iter() if local_name(e.tag) == "image"]
                at = list(node).index(child)
                node.remove(child)
                for offset, item in enumerate(replacements):
                    clean(item)
                    if local_name(item.tag) == "img":
                        node.insert(at + offset, item)
                continue
            if child_tag not in ALLOWED_TAGS:
                tail = child.tail or ""
                at = list(node).index(child)
                if at:
                    node[at - 1].tail = (node[at - 1].tail or "") + tail
                else:
                    node.text = (node.text or "") + tail
                node.remove(child)
            else:
                clean(child)
    if local_name(root.tag) != "html":
        raise ImportFailure("阅读章节缺少 HTML 根节点。")
    clean(root)
    # Fixed built-in styles only. Arbitrary book CSS could load remote resources.
    head = next((e for e in root if local_name(e.tag) == "head"), None)
    if head is not None:
        style = ET.SubElement(head, f"{{{XHTML}}}style")
        style.text = "body{line-height:1.8;overflow-wrap:anywhere}img{max-width:100%;height:auto}table{max-width:100%}pre{white-space:pre-wrap}"
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def normalize_epub(source, target):
    with zipfile.ZipFile(source) as archive:
        infos = archive.infolist()
        if len(infos) > MAX_ENTRIES or sum(i.file_size for i in infos) > MAX_EXPANDED:
            raise ImportFailure("EPUB 展开体积或资源数量超过处理上限。")
        names = set()
        for info in infos:
            name = info.filename
            if (name.startswith("/") or "\\" in name or ":" in name or ".." in PurePosixPath(name).parts
                    or any(ord(c) < 32 for c in name) or name in names
                    or stat.S_ISLNK(info.external_attr >> 16) or info.flag_bits & 1):
                raise ImportFailure("EPUB 含重复、加密或不安全的资源路径。")
            names.add(name)
            if info.file_size > MAX_ENTRY:
                raise ImportFailure("EPUB 单项资源超过 16 MB。")
        if "META-INF/encryption.xml" in names:
            raise ImportFailure("本阶段暂不支持加密或字体混淆 EPUB；原件已保留。")
        container = parse_xml(archive.read("META-INF/container.xml"))
        package_path = next((e.get("full-path") for e in container.iter() if local_name(e.tag) == "rootfile"), None)
        if package_path not in names:
            raise ImportFailure("EPUB 缺少有效书籍清单。")
        package = parse_xml(archive.read(package_path))
        manifest = next((e for e in package if local_name(e.tag) == "manifest"), None)
        spine = next((e for e in package if local_name(e.tag) == "spine"), None)
        if manifest is None or spine is None:
            raise ImportFailure("EPUB 缺少目录或阅读顺序。")
        items = {}
        for item in list(manifest):
            path = resolve_path(item.get("href", ""), package_path, names)
            media = item.get("media-type", "")
            if path is None or media not in MEDIA:
                manifest.remove(item)
                continue
            # Standalone SVG is not served; raster covers inside XHTML are retained.
            if media == "image/svg+xml":
                manifest.remove(item)
                continue
            if not item.get("id") or item.get("id") in items:
                raise ImportFailure("EPUB 清单资源 ID 重复或缺失。")
            items[item.get("id")] = (path, media)
            for attr in list(item.attrib):
                if attr not in {"id", "href", "media-type", "properties"}:
                    del item.attrib[attr]
        sections = []
        for item in spine:
            resource = items.get(item.get("idref"))
            if not resource or resource[1] not in {"application/xhtml+xml", "text/html"}:
                raise ImportFailure("EPUB 阅读章节使用了不支持的格式。")
            sections.append({"href": resource[0]})
        if not sections:
            raise ImportFailure("EPUB 没有可阅读章节。")
        allowed = {path for path, media in items.values()}
        resources = {}
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as result:
            def put(path, data):
                result.writestr(path, data)
                resources[path] = len(data)
            put("META-INF/container.xml", ET.tostring(container, encoding="utf-8"))
            put(package_path, ET.tostring(package, encoding="utf-8"))
            for path, media in items.values():
                if path in resources:
                    continue
                data = archive.read(path)
                if media in {"application/xhtml+xml", "text/html"}:
                    data = sanitize_xhtml(data, path, allowed)
                elif media == "text/css":
                    data = b"/* Reading layout uses the workbench style. */"
                elif media == "application/x-dtbncx+xml":
                    root = parse_xml(data)
                    for e in root.iter():
                        if local_name(e.tag) == "content" and not resolve_path(e.get("src", ""), path, allowed):
                            e.set("src", sections[0]["href"])
                    data = ET.tostring(root, encoding="utf-8")
                put(path, data)
    return resources, sections


def normalize_txt(source, target, title):
    if source.stat().st_size > 10 * 1024 * 1024:
        raise ImportFailure("TXT 暂支持 10 MB 以内文件。")
    raw = source.read_bytes()
    try:
        text = raw.decode("utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig")
    except UnicodeError as exc:
        raise ImportFailure("TXT 编码不明确，请转换为 UTF-8 后重新上传；原件已保留。") from exc
    if not text.strip() or any(ord(c) < 32 and c not in "\n\r\t" for c in text):
        raise ImportFailure("TXT 为空或含非文本控制字符。")
    # Fixed-size paragraphs/sections, never change after publishing this derivative.
    paragraphs = []
    for line in text.splitlines():
        if line.strip():
            paragraphs.extend(line[i:i+4000] for i in range(0, len(line), 4000))
    chunks, current, length = [], [], 0
    for line in paragraphs:
        current.append(line)
        length += len(line)
        if length >= 20000:
            chunks.append(current)
            current, length = [], 0
    if current:
        chunks.append(current)
    resources, sections = {}, []
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        def put(path, content):
            data = content.encode("utf-8")
            archive.writestr(path, data)
            resources[path] = len(data)
        put("META-INF/container.xml", '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0"><rootfiles><rootfile full-path="book.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        manifest, spine = [], []
        for index, lines in enumerate(chunks):
            path = f"part-{index}.xhtml"
            sections.append({"href": path})
            manifest.append(f'<item id="p{index}" href="{path}" media-type="application/xhtml+xml"/>')
            spine.append(f'<itemref idref="p{index}"/>')
            body = "".join(f'<p id="p{i}">{html.escape(line)}</p>' for i, line in enumerate(lines))
            put(path, f'<html xmlns="{XHTML}"><head><title>第 {index+1} 节</title></head><body>{body}</body></html>')
        put("book.opf", f'<package xmlns="{OPF}" version="3.0" unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="id">text-book</dc:identifier><dc:title>{html.escape(title)}</dc:title><dc:language>zh</dc:language></metadata><manifest>{"".join(manifest)}</manifest><spine>{"".join(spine)}</spine></package>')
    return resources, sections
