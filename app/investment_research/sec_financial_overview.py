"""Read-only navigation over saved SEC reports; no inferred financial values."""
import re
from django.db.models import OuterRef, Subquery
from django.db.models.functions import Substr
from .models import CompanyMaterialVersion


def report_info(version):
    data = version.data
    form = str(data.get("document_type", "")).upper()
    # A release filename alone cannot establish that the document reports earnings.
    headline = getattr(version, "headline", None)
    if headline is None:
        headline = (version.text or "")[:4000]
    announcement = re.search(
        r"(?:reports?|announces?).{0,180}(?:financial results|quarter.{0,70}results|full.year.{0,70}results)|(?:reports?|announces?|announced)\s+(?:record\s+)?(?:financial\s+)?results\s+for.{0,100}(?:quarter|fiscal|year)|业绩公告|财务业绩",
        headline, re.I | re.S)
    earnings = form in {"8-K", "6-K"} and bool(announcement)
    period = ""
    if earnings:
        match = re.search(r"(?:quarter|year|period).{0,100}?ended\s+([A-Za-z]+\s+\d{1,2},\s+\d{4})", headline, re.I)
        if match:
            from datetime import datetime
            try:
                period = datetime.strptime(match[1], "%B %d, %Y").date().isoformat()
            except ValueError:
                pass
    else:
        period = version.report_date or ""
    # Never mark an entire annual filing audited merely because its form is 10-K.
    audit_text = headline if 'text' in getattr(version, 'get_deferred_fields', lambda: set())() else version.text or ''
    audit = "未经审计（原文标注）" if earnings and re.search(r"\bunaudited\b|未经审计", audit_text, re.I) else "审计状态请见原文"
    return {"version": version, "form": form, "earnings": earnings,
            "period": period or "正文中核对", "filed": data.get("filing_date", ""), "audit": audit,
            "title": ("全年业绩公告" if re.search(r"full.year|全年", announcement.group(), re.I) else "业绩公告") if earnings else form}


def financial_overview(security):
    latest = CompanyMaterialVersion.objects.filter(material_id=OuterRef("material_id")).order_by("-number").values("pk")[:1]
    versions = CompanyMaterialVersion.objects.filter(material__security=security,
        material__kind="sec_document", pk=Subquery(latest)).select_related("material").defer("raw_gzip", "text").annotate(headline=Substr("text", 1, 4000))
    annual, quarterly, releases = [], [], []
    for version in versions:
        info = report_info(version)
        if info["earnings"]:
            releases.append(info)
        elif not version.data.get("attachment"):
            if info["form"] in {"10-K", "20-F", "40-F", "10-K/A", "20-F/A", "40-F/A"}:
                annual.append(info)
            elif info["form"] in {"10-Q", "10-Q/A"}:
                quarterly.append(info)
    key = lambda r: (r["filed"] or "", r["version"].pk)
    # Cover letters can repeat the earnings headline. Prefer the actual exhibit.
    by_accession = {}
    for item in sorted(releases, key=key, reverse=True):
        accession = item["version"].data.get("accession") or str(item["version"].pk)
        previous = by_accession.get(accession)
        if previous is None or (item["version"].data.get("attachment") and not previous["version"].data.get("attachment")):
            by_accession[accession] = item
    return {"releases": sorted(by_accession.values(), key=key, reverse=True)[:3],
            "annual": sorted(annual, key=lambda r: (r["period"], key(r)), reverse=True)[:1],
            "quarterly": sorted(quarterly, key=lambda r: (r["period"], key(r)), reverse=True)[:1]}
