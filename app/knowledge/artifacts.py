import hashlib
import html
import json
import re
import unicodedata
from collections import defaultdict
from html.parser import HTMLParser
from pathlib import Path

from django.core.files.base import ContentFile
from django.db import models, transaction
from django.urls import reverse

from intelligence.models import (
    SubjectKnowledgeIdentity,
    normalize_knowledge_author_name,
)

from .models import (
    KnowledgeArtifact,
    KnowledgeArtifactEvidence,
    KnowledgeArtifactVersion,
    KnowledgeDocument,
    KnowledgeSource,
    KnowledgeVisibility,
)
from .search import index_artifact


MAX_ARTIFACT_BYTES = 5 * 1024 * 1024
DATE_TITLE_PATTERN = re.compile(
    r"(?P<date_first>20\d{2}-\d{2}-\d{2})\s*《(?P<title_after>[^》]{1,500})》"
    r"|《(?P<title_before>[^》]{1,500})》\s*[（(](?P<date_after>20\d{2}-\d{2}-\d{2})[）)]"
)
DATA_PATTERN = re.compile(
    r"(?P<prefix>const\s+DATA\s*=\s*)(?P<data>\[.*?\])(?P<suffix>;\s*\n\s*const\s+state)",
    re.DOTALL,
)


class KnowledgeArtifactError(ValueError):
    pass


class _VisibleTextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag.lower() in {"script", "style", "noscript", "template"}:
            self.hidden_depth += 1

    def handle_endtag(self, tag):
        if tag.lower() in {"script", "style", "noscript", "template"}:
            self.hidden_depth = max(0, self.hidden_depth - 1)

    def handle_data(self, data):
        if not self.hidden_depth:
            value = " ".join(data.split())
            if value:
                self.parts.append(value)


def _decode_html(raw):
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise KnowledgeArtifactError("HTML 文件编码无法识别，请转换为 UTF-8 后重试。")


def _html_title(source):
    match = re.search(r"<title[^>]*>(.*?)</title>", source, re.I | re.S)
    if not match:
        return ""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", "", match.group(1))).split())


def _title_key(value):
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(
        character
        for character in normalized
        if not character.isspace()
        and not unicodedata.category(character).startswith(("P", "S"))
    )


def _reference_from_match(match):
    title = (match.group("title_after") or match.group("title_before") or "").strip()
    date_value = match.group("date_first") or match.group("date_after") or ""
    reference_key = hashlib.sha256(
        f"{date_value}|{_title_key(title)}".encode("utf-8")
    ).hexdigest()
    return {
        "key": reference_key,
        "text": match.group(0),
        "title": title,
        "date": date_value,
    }


def _iter_references(value):
    for match in DATE_TITLE_PATTERN.finditer(str(value or "")):
        yield _reference_from_match(match)


def _extract_data(source):
    match = DATA_PATTERN.search(source)
    if not match:
        return None, None
    try:
        return match, json.loads(match.group("data"))
    except json.JSONDecodeError as exc:
        raise KnowledgeArtifactError("知识体系图中的 DATA 数据无法解析。") from exc


def _iter_points(data):
    for category in data or []:
        for subtheme in category.get("subthemes") or []:
            for point in subtheme.get("points") or []:
                yield category, subtheme, point


def _extract_references_and_text(source):
    references = {}
    data_match, data = _extract_data(source)
    if data is not None:
        text_parts = []
        for category, subtheme, point in _iter_points(data):
            text_parts.extend(
                [
                    category.get("name", ""),
                    subtheme.get("name", ""),
                    point.get("title", ""),
                    point.get("yaodian", ""),
                    point.get("yuanwen", ""),
                    point.get("chuchu", ""),
                ]
            )
            for reference in _iter_references(point.get("chuchu", "")):
                references.setdefault(reference["key"], reference)
        plain_text = "\n".join(value for value in text_parts if value)
        return references, plain_text, data_match, data

    for cite_match in re.finditer(r"<cite\b[^>]*>(.*?)</cite>", source, re.I | re.S):
        citation_text = html.unescape(re.sub(r"<[^>]+>", "", cite_match.group(1)))
        for reference in _iter_references(citation_text):
            references.setdefault(reference["key"], reference)
    parser = _VisibleTextParser()
    parser.feed(source)
    return references, "\n".join(parser.parts), None, None


def _author_names(family, person_name):
    names = {person_name}
    identity = (
        SubjectKnowledgeIdentity.objects.filter(
            family=family,
            normalized_author_name=normalize_knowledge_author_name(person_name),
            is_active=True,
        )
        .select_related("subject")
        .first()
    )
    if identity:
        names.update(
            SubjectKnowledgeIdentity.objects.filter(
                family=family,
                subject=identity.subject,
                is_active=True,
            ).values_list("author_name", flat=True)
        )
    return {name.strip() for name in names if name and name.strip()}


def _candidate_documents(family, owner, person_name, visibility):
    queryset = (
        KnowledgeDocument.objects.filter(
            family=family,
            author__in=_author_names(family, person_name),
            source__kind__in=[
                KnowledgeSource.KIND_HTML_IMPORT,
                KnowledgeSource.KIND_MARKDOWN_IMPORT,
            ],
            current_revision__isnull=False,
        )
        .select_related("current_revision")
        .order_by("id")
    )
    if visibility == KnowledgeVisibility.FAMILY:
        queryset = queryset.filter(
            visibility=KnowledgeVisibility.FAMILY,
            source__visibility=KnowledgeVisibility.FAMILY,
        )
    else:
        queryset = queryset.filter(
            models.Q(owner=owner)
            | models.Q(
                visibility=KnowledgeVisibility.FAMILY,
                source__visibility=KnowledgeVisibility.FAMILY,
            )
        )
    return list(queryset)


def _document_dates(document):
    return {
        value.date().isoformat()
        for value in [document.content_created_at, document.content_modified_at]
        if value
    }


def _match_reference(reference, by_title):
    candidates = list(by_title.get(_title_key(reference["title"]), []))
    dated = [
        document
        for document in candidates
        if reference["date"] in _document_dates(document)
    ]
    if len(dated) == 1:
        return dated[0], "title_and_date", []
    if len(dated) > 1:
        return None, "", [document.pk for document in dated]
    if len(candidates) == 1:
        return candidates[0], "unique_title", []
    if len(candidates) > 1:
        return None, "", [document.pk for document in candidates]
    return None, "", []


def _evidence_style_and_script():
    return """
<style>
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei","PingFang SC",sans-serif!important}
.knowledge-evidence-link,.knowledge-evidence-button{color:#0f766e!important;text-decoration:underline;text-decoration-thickness:1px;text-underline-offset:3px;cursor:pointer}
.knowledge-evidence-button{appearance:none;border:0;background:transparent;padding:0;font:inherit}
.knowledge-evidence-unmatched{border-bottom:1px dashed #b7791f;color:#8a6212;cursor:help}
.knowledge-evidence-ambiguous{border-bottom:1px dashed #b45309;color:#9a4b0c;cursor:help}
</style>
<script>
function openKnowledgeEvidence(url){
  window.parent.postMessage({type:"knowledge-artifact-evidence",url:url},"*");
}
</script>
"""


def _family_theme_style(source):
    """Return a family-workbench theme override for the two imported artifacts.

    The uploaded HTML is kept byte-for-byte as the original. This override only
    affects the isolated display copy, so the original download remains useful
    for audit and regeneration. The marker makes the operation idempotent when
    an already-rendered version is served again.
    """
    if 'data-knowledge-family-theme="v1"' in source:
        return ""
    is_manual = "道势法术心" in source and bool(
        re.search(r'class=["\'][^"\']*\bsidebar\b', source, flags=re.I)
    )
    is_mind_map = bool(
        re.search(r"(?:#mindmap-card|id=[\"']mindmap-card)", source, flags=re.I)
    ) and "function renderTree" in source
    if not is_manual and not is_mind_map:
        return ""
    manual_override = """
<style data-knowledge-family-theme="v1">
:root{
  --ink:#173a37;--ink-soft:#526963;--paper:#f5f8f6;--paper-2:#e9f2ee;
  --card:#fff;--cinnabar:#0f766e;--cinnabar-d:#0b5c54;
  --gold:#5b9d92;--gold-l:#2f7f75;--pine:#285c54;--line:#dbe5e3;
  --sidebar:#fff;--sidebar-t:#52615d;--quote:#ecfdf5;
  --shadow:0 10px 30px rgba(34,67,59,.08)
}
.sidebar{background:linear-gradient(180deg,#fff 0%,#f8fbf9 100%);color:var(--sidebar-t);border-right-color:#dbe5e3}
.brand{color:#0f766e}.brand-sub{color:#7b8b87}
.search{border-color:#dbe5e3;background:#f8faf8;color:#173a37}
.search:focus{border-color:#0f766e}
.nav a{color:#52615d}.nav a:hover,.nav a.active{background:#ecfdf5;color:#0f766e}
.nav .l1{color:#0f766e}.nav .count{color:#8a9692}
.disclaimer{background:#ecfdf5;border-left-color:#0f766e;color:#365d57}
h2{border-bottom-color:#dbe5e3}h3{color:#0b5c54}h4{color:#285c54}
blockquote{background:#f3faf7;border-left-color:#72b8aa;color:#365d57}
cite{color:#68817b}.tag{border-color:#cfe1dc;color:#52716b;background:#f1f7f5}
.kicker{color:#3f8f82}.stat b{color:#0b5c54}
hr.orn{background:linear-gradient(90deg,transparent,#72b8aa,transparent)}
</style>
"""
    if is_manual:
        return manual_override
    return """
<style data-knowledge-family-theme="v1">
:root{--bg:#f5f8f6;--card:#fff;--ink:#173a37;--muted:#6b7a76;--line:#dbe5e3;--accent:#0f766e}
body{background:var(--bg);color:var(--ink)}
header{background:linear-gradient(135deg,#172554 0%,#0f766e 68%,#2f8f82 100%)}
header .sub{color:#d5ebe5}header .sub b{color:#fff}
.search-wrap{box-shadow:0 6px 24px rgba(34,67,59,.14)}
.toolbar button{border-color:rgba(255,255,255,.38);background:rgba(255,255,255,.16)}
.toolbar button:hover{background:rgba(255,255,255,.28)}
#mindmap-card{border-color:#dbe5e3;box-shadow:0 2px 10px rgba(34,67,59,.06)}
.mm-label{fill:#365d57}.mm-sub{fill:#8a9b96}
.cat-body{box-shadow:0 2px 10px rgba(34,67,59,.06)}
.sub-name{color:var(--ink)}.sub-count{background:#eef5f2;color:#71837d}
.point-title{color:#173a37}.point-yaodian{color:#42615b}
.point-yuanwen{color:#526963;border-color:#dfeae6}
.point-yuanwen::before{background:#fff;color:#7b8f89}
.point-chuchu,.point-chuchu .src{color:#728882}
mark{background:#e4f4ee;color:#174e47}
.legend,.footer{color:#7b8f89}
</style>
"""


def apply_family_theme(source):
    """Apply the family-workbench visual treatment to supported artifacts."""
    injection = _family_theme_style(source)
    if not injection:
        return source
    if re.search(r"</head>", source, flags=re.I):
        return re.sub(r"</head>", f"{injection}</head>", source, count=1, flags=re.I)
    return injection + source


def _reference_markup(reference, evidence):
    text = html.escape(reference["text"])
    if evidence.status == KnowledgeArtifactEvidence.STATUS_MATCHED:
        url = reverse("knowledge:artifact_evidence", kwargs={"pk": evidence.pk})
        version_label = evidence.revision.revision_number if evidence.revision_id else "-"
        return (
            f'<a href="#" class="knowledge-evidence-link" '
            f'onclick="openKnowledgeEvidence(\'{url}\');return false" '
            f'title="打开知识中心原文 · 固定证据版本 v{version_label}">{text}</a>'
        )
    if evidence.status == KnowledgeArtifactEvidence.STATUS_AMBIGUOUS:
        return (
            f'<span class="knowledge-evidence-ambiguous" title="找到多个候选原文，待人工核对">'
            f"{text}</span>"
        )
    return (
        f'<span class="knowledge-evidence-unmatched" title="当前未找到对应原文">'
        f"{text}</span>"
    )


def _replace_reference_text(value, evidence_by_key):
    def replace(match):
        reference = _reference_from_match(match)
        evidence = evidence_by_key.get(reference["key"])
        return _reference_markup(reference, evidence) if evidence else html.escape(match.group(0))

    return DATE_TITLE_PATTERN.sub(replace, str(value or ""))


def _render_manual(source, evidence_by_key):
    def replace_cite(match):
        inner = html.unescape(re.sub(r"<[^>]+>", "", match.group(1)))
        return f"<cite>{_replace_reference_text(inner, evidence_by_key)}</cite>"

    rendered = re.sub(r"<cite\b[^>]*>(.*?)</cite>", replace_cite, source, flags=re.I | re.S)
    injection = _evidence_style_and_script()
    if "</head>" in rendered.lower():
        rendered = re.sub(r"</head>", f"{injection}</head>", rendered, count=1, flags=re.I)
    else:
        rendered = injection + rendered
    return apply_family_theme(rendered)


def _render_mind_map(source, data_match, data, evidence_by_key):
    for _, _, point in _iter_points(data):
        point["_evidence"] = [
            {
                "text": reference["text"],
                "status": evidence_by_key[reference["key"]].status,
                "url": (
                    reverse(
                        "knowledge:artifact_evidence",
                        kwargs={"pk": evidence_by_key[reference["key"]].pk},
                    )
                    if evidence_by_key[reference["key"]].status
                    == KnowledgeArtifactEvidence.STATUS_MATCHED
                    else ""
                ),
            }
            for reference in _iter_references(point.get("chuchu", ""))
            if reference["key"] in evidence_by_key
        ]
    data_json = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    rendered = (
        source[: data_match.start("data")]
        + data_json
        + source[data_match.end("data") :]
    )
    helper = """
function renderEvidence(point){
  const refs=point._evidence||[];
  if(!refs.length){return point.chuchu?`<div class="point-chuchu">📚 出处：<span class="src">${esc(point.chuchu)}</span></div>`:"";}
  const parts=refs.map(ref=>{
    const label=esc(ref.text);
    if(ref.status==="matched"){return `<button type="button" class="knowledge-evidence-button" onclick="openKnowledgeEvidence('${ref.url}')" title="打开知识中心原文">${label}</button>`;}
    const cls=ref.status==="ambiguous"?"knowledge-evidence-ambiguous":"knowledge-evidence-unmatched";
    const tip=ref.status==="ambiguous"?"找到多个候选原文，待人工核对":"当前未找到对应原文";
    return `<span class="${cls}" title="${tip}">${label}</span>`;
  });
  return `<div class="point-chuchu">📚 出处：<span class="src">${parts.join("；")}</span></div>`;
}
"""
    rendered = rendered.replace("function renderTree(){", helper + "\nfunction renderTree(){", 1)
    rendered = re.sub(
        r'const chuchu\s*=\s*p\.chuchu\s*\?\s*`<div class="point-chuchu">.*?</div>`\s*:\s*""\s*;',
        "const chuchu = renderEvidence(p);",
        rendered,
        count=1,
    )
    injection = _evidence_style_and_script()
    rendered = re.sub(r"</head>", f"{injection}</head>", rendered, count=1, flags=re.I)
    return apply_family_theme(rendered)


@transaction.atomic
def create_or_update_artifact(*, family, owner, cleaned_data):
    uploaded = cleaned_data["html_file"]
    raw = uploaded.read()
    if len(raw) > MAX_ARTIFACT_BYTES:
        raise KnowledgeArtifactError("HTML 文件不能超过 5 MB。")
    source = _decode_html(raw)
    if "<html" not in source[:10000].casefold():
        raise KnowledgeArtifactError("文件不是可识别的 HTML 文档。")
    references, plain_text, data_match, data = _extract_references_and_text(source)
    title = cleaned_data.get("title") or _html_title(source)
    if not title:
        raise KnowledgeArtifactError("HTML 没有页面标题，请手工填写成果标题。")
    artifact, created = KnowledgeArtifact.objects.get_or_create(
        family=family,
        normalized_person_name=normalize_knowledge_author_name(
            cleaned_data["person_name"]
        ),
        artifact_type=cleaned_data["artifact_type"],
        defaults={
            "owner": owner,
            "person_name": cleaned_data["person_name"],
            "title": title,
            "description": cleaned_data.get("description", ""),
            "visibility": cleaned_data["visibility"],
        },
    )
    if not created and artifact.owner_id != owner.pk:
        raise KnowledgeArtifactError("这个人物的同类成果由另一名成员管理，不能直接覆盖。")
    content_hash = hashlib.sha256(raw).hexdigest()
    existing = artifact.versions.filter(content_hash=content_hash).first()
    if existing:
        artifact.current_version = existing
        artifact.save(update_fields=["current_version", "updated_at"])
        index_artifact(artifact)
        return artifact, existing, False

    artifact.person_name = cleaned_data["person_name"]
    artifact.title = title
    artifact.description = cleaned_data.get("description", "")
    artifact.visibility = cleaned_data["visibility"]
    artifact.status = KnowledgeArtifact.STATUS_PENDING
    artifact.confirmed_by = None
    artifact.confirmed_at = None
    artifact.save()

    version_number = (artifact.versions.order_by("-version_number").values_list("version_number", flat=True).first() or 0) + 1
    version = KnowledgeArtifactVersion(
        artifact=artifact,
        version_number=version_number,
        original_name=Path(uploaded.name).name[:300],
        content_hash=content_hash,
        byte_size=len(raw),
        plain_text=plain_text,
        generator_name=cleaned_data.get("generator_name", ""),
        model_name=cleaned_data.get("model_name", ""),
        prompt_version=cleaned_data.get("prompt_version", ""),
        generated_at=cleaned_data.get("generated_at"),
        source_article_count=cleaned_data.get("source_article_count") or 0,
        source_cutoff_date=cleaned_data.get("source_cutoff_date"),
        created_by=owner,
    )
    version.original_file.save(version.original_name, ContentFile(raw), save=False)
    version.save()

    candidates = _candidate_documents(
        family,
        owner,
        artifact.person_name,
        artifact.visibility,
    )
    by_title = defaultdict(list)
    for document in candidates:
        by_title[_title_key(document.title)].append(document)

    evidence_by_key = {}
    for reference in references.values():
        document, method, candidate_ids = _match_reference(reference, by_title)
        status = KnowledgeArtifactEvidence.STATUS_UNMATCHED
        if document:
            status = KnowledgeArtifactEvidence.STATUS_MATCHED
        elif candidate_ids:
            status = KnowledgeArtifactEvidence.STATUS_AMBIGUOUS
        evidence = KnowledgeArtifactEvidence.objects.create(
            version=version,
            reference_key=reference["key"],
            citation_text=reference["text"],
            citation_title=reference["title"],
            citation_date=reference["date"] or None,
            status=status,
            match_method=method,
            document=document,
            revision=document.current_revision if document else None,
            candidate_document_ids=candidate_ids,
        )
        evidence_by_key[reference["key"]] = evidence

    if data is not None:
        rendered = _render_mind_map(source, data_match, data, evidence_by_key)
    else:
        rendered = _render_manual(source, evidence_by_key)
    version.rendered_file.save(
        f"{Path(version.original_name).stem}-linked.html",
        ContentFile(rendered.encode("utf-8")),
        save=False,
    )
    matched = [
        evidence
        for evidence in evidence_by_key.values()
        if evidence.status == KnowledgeArtifactEvidence.STATUS_MATCHED
    ]
    version.reference_count = len(evidence_by_key)
    version.matched_reference_count = len(matched)
    version.ambiguous_reference_count = sum(
        evidence.status == KnowledgeArtifactEvidence.STATUS_AMBIGUOUS
        for evidence in evidence_by_key.values()
    )
    version.unmatched_reference_count = sum(
        evidence.status == KnowledgeArtifactEvidence.STATUS_UNMATCHED
        for evidence in evidence_by_key.values()
    )
    version.source_snapshot = [
        {
            "document_id": evidence.document_id,
            "revision_id": evidence.revision_id,
            "content_hash": evidence.revision.content_hash,
        }
        for evidence in matched
    ]
    version.save(
        update_fields=[
            "rendered_file",
            "reference_count",
            "matched_reference_count",
            "ambiguous_reference_count",
            "unmatched_reference_count",
            "source_snapshot",
        ]
    )
    artifact.current_version = version
    artifact.save(update_fields=["current_version", "updated_at"])
    index_artifact(artifact)
    return artifact, version, True
