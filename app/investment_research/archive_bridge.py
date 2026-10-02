"""Link saved SEC originals into the shared research archive without fetching.

Both archives remain append-only. Existing citations keep their IDs; the shared
archive is the reference used by financial parsing, research and filing reviews.
This service runs on explicit acquisition or migration, never on a GET page.
"""
import gzip
import hashlib
import re
from datetime import date
from urllib.parse import urlsplit

from django.db import transaction
from django.db.models import OuterRef, Subquery


def _date(value):
    try:
        return date.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None


def link_sec_version(version, *, apps=None, using="default"):
    if apps is None:
        from django.apps import apps
    Document = apps.get_model("investment_research", "OfficialResearchDocument")
    Content = apps.get_model("investment_research", "OfficialResearchContentVersion")
    material, record = version.material, version.data
    if material.kind != "sec_document" or not version.text.strip():
        return None, False
    url = version.source_url
    parts = urlsplit(url)
    path = re.fullmatch(r"/Archives/edgar/data/(\d+)/(\d{18})/([A-Za-z0-9_.-]+)", parts.path)
    accession = str(record.get("accession") or "")
    if not url or not re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession):
        # Unidentified imported records stay in their original archive; a title
        # alone cannot establish an official issuer/filing identity.
        return None, False
    if (parts.scheme != "https" or parts.netloc != "www.sec.gov" or not path
            or parts.query or parts.fragment
            or not re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession)
            or accession.replace("-", "") != path[2]):
        raise ValueError("已保存 SEC 原件的链接与申报身份不一致，停止建立引用。")
    raw = gzip.decompress(bytes(version.raw_gzip))
    if hashlib.sha256(raw).hexdigest() != version.sha256:
        raise ValueError("已保存 SEC 原件校验失败，停止建立引用。")
    attachment = str(record.get("attachment") or "")
    form = str(record.get("document_type") or "other").lower()
    period_end = _date(record.get("report_date"))
    if attachment:
        from .sec_financial_overview import report_info
        info = report_info(version)
        external_id = accession + ":exhibit:" + hashlib.sha256(attachment.encode()).hexdigest()[:32]
        document_type = "earnings_release" if info["earnings"] else "other"
        # An 8-K filing date is not the fiscal period of its earnings exhibit.
        period_end = _date(info["period"]) if info["earnings"] else None
    else:
        external_id, document_type = accession, form
    digest = hashlib.sha256(version.text.encode()).hexdigest()
    with transaction.atomic(using=using):
        doc, _ = Document.objects.using(using).get_or_create(source="sec", external_id=external_id,
            defaults={"security_id": material.security_id, "document_type": document_type,
                      "title": material.title, "source_url": url,
                      "published_at": _date(record.get("filing_date")),
                      "period_end": period_end,
                      "metadata": {**record, "cik": path[1].zfill(10), "company_material_id": material.pk}})
        doc = Document.objects.using(using).select_for_update().get(pk=doc.pk)
        if doc.security_id != material.security_id or doc.source_url != url:
            raise ValueError("SEC 申报已关联其他证券或原件链接，停止建立引用。")
        existing = Content.objects.using(using).filter(document=doc,
            raw_sha256=version.sha256, content_sha256=digest).first()
        if existing:
            return existing, False
        latest = Content.objects.using(using).filter(document=doc).order_by("-version_number").first()
        if latest and version.fetched_at < latest.fetched_at:
            # The original remains in the company archive. Do not make an older
            # original appear to be the newest research version by appending it.
            return latest, False
        linked = Content.objects.using(using).create(document=doc,
            version_number=latest.version_number + 1 if latest else 1,
            source_url=url, raw_sha256=version.sha256, raw_gzip=version.raw_gzip,
            media_type=version.media_type, content_text=version.text, content_sha256=digest,
            extractor_version="company-archive-v1", fetched_at=version.fetched_at,
            acquisition_note=f"沿用公司资料库原件：资料 {material.pk}，版本 {version.pk}，第 {version.number} 版")
        # An older archive must not replace a newer explicitly acquired original.
        if not doc.fetched_at or version.fetched_at >= doc.fetched_at:
            doc.content_text, doc.content_sha256, doc.fetched_at = version.text, digest, version.fetched_at
            doc.metadata = {**doc.metadata, "company_material_id": material.pk}
            doc.save(using=using, update_fields=["content_text", "content_sha256", "fetched_at", "metadata", "updated_at"])
        return linked, True


def link_saved_sec_materials(apps, schema_editor):
    Version = apps.get_model("investment_research", "CompanyMaterialVersion")
    using = schema_editor.connection.alias
    latest = Version.objects.using(using).filter(material_id=OuterRef("material_id")).order_by("-number").values("pk")[:1]
    versions = Version.objects.using(using).filter(material__kind="sec_document", pk=Subquery(latest)).exclude(
        text="").select_related("material").order_by("fetched_at", "pk")
    for version in versions.iterator(chunk_size=20):
        link_sec_version(version, apps=apps, using=using)
