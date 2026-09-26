"""Immutable reading publications. Uploaded HTML is never interpreted as instructions."""
import hashlib
import json
import re
from html import escape
from html.parser import HTMLParser
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from .models import Book, ReadingArtifact, ReadingArtifactVersion, ReadingArchive
from .storage import storage

MAX_ARTIFACT_BYTES = 5 * 1024 * 1024
LABELS = {"book":"原书观点", "member":"成员观点", "ai":"AI 提炼", "external":"外部整理"}


class DataParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.capture = False
        self.parts = []
        self.count = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script" and attrs.get("id") == "reading-artifact-data":
            if attrs.get("type") != "application/json":
                raise ValidationError("标准成果的数据块必须为 application/json。")
            self.capture = True
            self.count += 1

    def handle_endtag(self, tag):
        if tag == "script": self.capture = False

    def handle_data(self, data):
        if self.capture: self.parts.append(data)


def canonical(data):
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def text(value, limit, required=False):
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise ValidationError("成果文本缺失或超过长度限制。")
    return value.strip()


def validate_data(data, trusted_sources=None, ai_result=False):
    if not isinstance(data, dict) or type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise ValidationError("目前支持 schema_version 为 1 的成果结构。")
    result = {"schema_version":1, "title":text(data.get("title"),250,True), "sources":[], "sections":[], "nodes":[]}
    for key, maximum in (("sources",100),("sections",200),("nodes",200)):
        items = data.get(key, [])
        if not isinstance(items,list) or len(items)>maximum:
            raise ValidationError("成果结构超过条目数量限制。")
        ids=set()
        for item in items:
            if not isinstance(item,dict): raise ValidationError("成果条目须为对象。")
            identity=item.get("id")
            if not isinstance(identity,str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}",identity) or identity in ids:
                raise ValidationError("小节、引用和节点必须有各自唯一的稳定 ID。")
            ids.add(identity)
            if key=="sources":
                if trusted_sources is not None:
                    if identity not in trusted_sources: raise ValidationError("AI 引用了输入范围之外的资料。")
                    clean={**trusted_sources[identity],"id":identity}
                else:
                    # An external claim cannot grant access or certify provenance.
                    clean={"id":identity,"kind":"external","quote":text(item.get("quote",""),12000),
                           "label":text(item.get("label","外部提供的依据"),250),"verified":False}
                result[key].append(clean)
            else:
                refs=item.get("source_ids",[])
                if not isinstance(refs,list) or any(not isinstance(x,str) for x in refs) or len(refs)>100:
                    raise ValidationError("引用列表无效。")
                clean={"id":identity,"title":text(item.get("title"),250,True),"source_ids":list(dict.fromkeys(refs))}
                if key=="sections":
                    kind=item.get("kind","external")
                    if kind not in LABELS: raise ValidationError("观点类型无效。")
                    clean.update(content=text(item.get("content",""),12000),kind=kind)
                else:
                    clean.update(parent=text(item.get("parent", ""),64),section_id=text(item.get("section_id",""),64))
                result[key].append(clean)
    source_ids={s["id"] for s in result["sources"]}
    sections={s["id"]:s for s in result["sections"]}
    nodes={n["id"]:n for n in result["nodes"]}
    if not sections: raise ValidationError("标准成果至少需要一个正文小节。")
    for item in [*sections.values(),*nodes.values()]:
        if not set(item["source_ids"])<=source_ids: raise ValidationError("成果包含不存在的引用。")
        if ai_result and not item["source_ids"]:
            raise ValidationError("AI 每个小节和节点都须引用所选输入。")
        if ai_result and item.get("kind") in {"book","member"}:
            if any(trusted_sources[x]["kind"]!=item["kind"] for x in item["source_ids"]):
                raise ValidationError("AI 混淆了原书和成员观点。")
    for node in nodes.values():
        if node["section_id"] and node["section_id"] not in sections: raise ValidationError("节点关联的小节不存在。")
        seen={node["id"]};parent=node["parent"]
        while parent:
            if parent in seen or parent not in nodes or len(seen)>20: raise ValidationError("导图包含循环、无效父节点或层级超过 20。")
            seen.add(parent);parent=nodes[parent]["parent"]
    if len(canonical(result))>300000: raise ValidationError("标准成果文本总量过大。")
    return result


def read_upload(upload):
    if upload.size>MAX_ARTIFACT_BYTES: raise ValidationError("成果 HTML 上限为 5 MB。")
    if not upload.name.lower().endswith((".html",".htm")): raise ValidationError("请上传单个 HTML 文件。")
    raw=upload.read(MAX_ARTIFACT_BYTES+1)
    if len(raw)>MAX_ARTIFACT_BYTES: raise ValidationError("成果 HTML 上限为 5 MB。")
    try:
        source=raw.decode("utf-8-sig");parser=DataParser();parser.feed(source)
        if parser.count>1: raise ValidationError("标准成果只能有一个数据块。")
        data=validate_data(json.loads("".join(parser.parts))) if parser.count else {}
    except (UnicodeError,ValueError,RecursionError) as exc:
        raise ValidationError("请使用 UTF-8 HTML；内嵌 JSON 须为完整的标准结构。") from exc
    return raw,data


def body_html(data):
    parts=[]
    for section in data.get("sections",[]):
        parts.append(f'<section id="{section["id"]}"><h2>{escape(section["title"])}</h2><p>{LABELS[section["kind"]]}</p><p>{escape(section["content"]).replace(chr(10),"<br>")}</p></section>')
    return "\n".join(parts)


def export_html(data):
    payload=canonical(data).replace("<","\\u003c")
    return (f'<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{escape(data["title"])}</title>'
            f'<h1>{escape(data["title"])}</h1>{body_html(data)}<script type="application/json" id="reading-artifact-data">{payload}</script></html>').encode()


def publish(book,member,title,visibility,raw,data,kind="external",artifact=None,base_version=None,name="成果.html"):
    digest=hashlib.sha256(raw).hexdigest()
    saved=""
    try:
        with transaction.atomic():
            # Serialize creation and duplicates for one book, never deduplicate across owners.
            from .permissions import accessible_books
            if not accessible_books(member).select_for_update(of=("self",)).filter(pk=book.pk).exists():
                raise ValidationError("图书访问权限已变化。")
            if artifact:
                artifact=ReadingArtifact.objects.select_for_update().get(pk=artifact.pk,owner=member,book=book)
                if artifact.current_version_id!=base_version: raise ValidationError("成果已更新，请刷新后基于最新版本编辑。")
            else:
                existing=ReadingArtifactVersion.objects.filter(artifact__book=book,artifact__owner=member,sha256=digest).first()
                if existing:return existing,False
                artifact=ReadingArtifact.objects.create(book=book,owner=member,title=title,visibility=visibility)
            existing=artifact.versions.filter(sha256=digest).first()
            if existing:return existing,False
            saved=storage().save(f"artifacts/{artifact.pk}/{uuid4().hex}.html",ContentFile(raw))
            version=ReadingArtifactVersion.objects.create(artifact=artifact,number=(artifact.current_version.number+1 if artifact.current_version else 1),
                title=title,data=data,original_path=saved,original_name=name[:250],sha256=digest,kind=kind)
            artifact.current_version=version;artifact.title=title;artifact.save()
            return version,True
    except Exception:
        if saved:storage().delete(saved)
        raise


def archive_version(version,member):
    from knowledge.models import KnowledgeDocument, KnowledgeRevision, KnowledgeSource
    from knowledge.search import index_document
    with transaction.atomic():
        from .permissions import accessible_books
        if not accessible_books(member).select_for_update(of=("self",)).filter(pk=version.artifact.book_id).exists():
            raise ValidationError("图书访问权限已变化。")
        artifact=ReadingArtifact.objects.select_for_update().get(pk=version.artifact_id,owner=member)
        previous=ReadingArchive.objects.filter(version=version).first()
        if previous:return previous.document
        source,_=KnowledgeSource.objects.get_or_create(family=member.family,key=f"reading:{member.pk}",defaults={
            "owner":member,"kind":KnowledgeSource.KIND_READING,"name":"阅读成果","visibility":"family","allow_cloud_ai":False})
        link=reverse("reading:artifact_version",args=[artifact.pk,version.number])
        html=f'<p>阅读成果发布版本 v{version.number} · <a href="{link}">查看导图、依据及原文件</a></p>'+body_html(version.data)
        plain="\n".join(f'{x["title"]}\n{LABELS[x["kind"]]}\n{x["content"]}' for x in version.data.get("sections",[]))
        doc=KnowledgeDocument.objects.create(family=member.family,source=source,owner=member,external_id=f"reading-version:{version.pk}",
            title=f"{version.title} · v{version.number}",author=member.display_name,visibility=artifact.visibility,curation_status="confirmed",knowledge_status="included",
            content_created_at=version.created_at,content_modified_at=version.created_at)
        revision=KnowledgeRevision.objects.create(document=doc,revision_number=1,content_hash=version.sha256,
            raw_file=f"reading/{version.original_path}",normalized_html=html,plain_text=plain,converter_version="reading-publication-1")
        doc.current_revision=revision;doc.save(update_fields=["current_revision","updated_at"])
        ReadingArchive.objects.create(version=version,document=doc);index_document(doc)
        return doc
