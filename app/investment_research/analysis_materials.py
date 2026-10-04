"""A bounded, cited research packet prepared from saved official materials."""

from datetime import timedelta
from decimal import Decimal

from django.db.models import F
from django.db.models.functions import Coalesce

from .citations import quote_digest
from .financial_overview import build_financial_overview
from .models import OfficialResearchContentVersion
from .official_ir import documents_for_security
from .tenk_chapters import tenk_chapter_coverage
from portfolio.research_quotes import saved_research_quote, freeze_research_quote


NARRATIVE_TERMS = ("revenue", "growth", "demand", "cash", "margin", "cloud",
                   "customer", "segment", "capital expenditure", "guidance",
                   "lease", "depreciation", "investment", "outlook")
NARRATIVE_TYPES = {"10-q", "earnings_release", "prepared_remarks", "transcript",
                   "shareholder_letter", "investor_update"}


def source_preview(dossier):
    """Cheap GET-page preview; no extraction, network access, or writes."""
    base = (OfficialResearchContentVersion.objects.filter(
        document__in=documents_for_security(dossier.security),
    ).exclude(content_text="").select_related("document").defer("raw_gzip", "content_text"))
    annual = (base.filter(document__source="sec", document__document_type="10-k")
              .order_by(F("document__period_end").desc(nulls_last=True), "-pk").first())
    recent = []
    seen = set()
    versions = (base.filter(document__document_type__in=NARRATIVE_TYPES)
                .order_by(Coalesce("document__period_end", "document__published_at").desc(nulls_last=True),
                          F("document__published_at").desc(nulls_last=True), "-pk")[:20])
    for version in versions:
        if version.document_id in seen:
            continue
        seen.add(version.document_id)
        recent.append(version)
        if len(recent) == 3:
            break
    return ([annual] if annual else []) + recent


def _citations(cell):
    if cell.get("citation") and cell.get("version_id") and cell.get("document_id"):
        cite = cell["citation"]
        return [{"version_id": cell["version_id"], "document_id": cell["document_id"],
                 "start": cite["start"], "end": cite["end"], "hash": cite["hash"]}]
    citations = []
    for component in cell.get("components", []):
        citations.extend(_citations(component["cell"]))
    return list({(c["version_id"], c["start"], c["end"]): c for c in citations}.values())


def _number(amount):
    return format(amount.quantize(Decimal("0.01")), "f")


def _narrative(version, *, start, end, count):
    text = version.content_text
    candidates = []
    offset = start
    for line in text[start:end].splitlines(keepends=True):
        stripped = line.strip()
        line_start = offset + len(line) - len(line.lstrip())
        offset += len(line)
        if len(stripped) < 65:
            continue
        quote = stripped[:350]
        priority = sum(term in quote.lower() for term in NARRATIVE_TERMS)
        if priority:
            candidates.append((priority, line_start, quote))
    candidates.sort(key=lambda item: (-item[0], item[1]))
    return sorted(candidates[:count], key=lambda item: item[1])


def prepare_analysis_materials(dossier):
    """Include all verified overview cells and a small, traceable narrative digest."""
    preview = source_preview(dossier)
    annual = next((v for v in preview if v.document.source == "sec"
                   and v.document.document_type == "10-k"), None)
    evidence, sources, periods = [], [], []
    valuation_basis = {}
    market_context = freeze_research_quote(saved_research_quote(dossier.security))
    problem = ""

    def add(version, text, citations):
        if not citations:
            return
        evidence.append({"id": f"E{len(evidence) + 1}", "text": text,
                         "citations": citations})

    if annual:
        annual = OfficialResearchContentVersion.objects.select_related("document").get(pk=annual.pk)
        historical = []
        if annual.document.period_end:
            historical = list(OfficialResearchContentVersion.objects.filter(
                document__security=dossier.security, document__source="sec",
                document__document_type="10-k",
                document__period_end__lt=annual.document.period_end,
                document__period_end__gte=annual.document.period_end - timedelta(days=900),
            ).select_related("document").order_by("-version_number", "-pk"))
        fiscal_periods, rows, problem = build_financial_overview(annual, historical)
        periods = [period.isoformat() for period in fiscal_periods]
        eps_row = next((row for row in rows if row["code"] == "diluted_eps"), None)
        if eps_row:
            for period, cell in reversed(list(zip(fiscal_periods, eps_row["cells"]))):
                citations = _citations(cell)
                if cell.get("amount") is not None and cell["amount"] > 0 and citations:
                    valuation_basis = {"eps": str(cell["amount"]),
                                       "period_end": period.isoformat(),
                                       "citation": citations[0]}
                    break
        for row in rows:
            for period, cell in zip(fiscal_periods, row["cells"]):
                amount = cell.get("amount")
                if amount is None:
                    continue
                citations = _citations(cell)
                if not citations:
                    continue
                unit = {"money": "亿美元", "shares": "亿股", "percent": "%",
                        "per_share": "美元/股"}.get(row["unit"], row["unit"])
                label = "计算值" if cell.get("derived") else "年报原值"
                add(annual, f"财年截至 {period}: {row['label']} {_number(amount)} {unit}（{label}）",
                    citations)
        sources.append({"document_id": annual.document_id, "version_id": annual.pk,
                        "document_title": annual.document.title,
                        "source": annual.document.get_source_display(),
                        "period_end": str(annual.document.period_end or "")})
        chapter = next((c for c in tenk_chapter_coverage(annual)
                        if c["code"] == "7" and c["located"]), None)
        if chapter:
            for _, start, quote in _narrative(annual, start=chapter["start"],
                                              end=chapter["end"], count=4):
                add(annual, f"{annual.document.title}（截至 {annual.document.period_end}）管理层讨论摘录：{quote}", [{
                    "version_id": annual.pk, "document_id": annual.document_id,
                    "start": start, "end": start + len(quote), "hash": quote_digest(quote),
                }])
    for preview_version in preview:
        if annual and preview_version.pk == annual.pk:
            continue
        version = OfficialResearchContentVersion.objects.select_related("document").get(
            pk=preview_version.pk)
        sources.append({"document_id": version.document_id, "version_id": version.pk,
                        "document_title": version.document.title,
                        "source": version.document.get_source_display(),
                        "period_end": str(version.document.period_end or "")})
        from .financial_excerpts import statement_excerpts
        for quote, start in statement_excerpts(version.content_text):
            add(version, f"{version.document.title} 财务报表原文摘录（按表头日期、期间与单位逐列阅读）：{quote}", [{
                "version_id": version.pk, "document_id": version.document_id,
                "start": start, "end": start + len(quote), "hash": quote_digest(quote),
            }])
        for _, start, quote in _narrative(version, start=0,
                                          end=min(len(version.content_text), 20000), count=4):
            source_date = version.document.period_end or version.document.published_at or "日期未标明"
            add(version, f"{version.document.title}（{source_date}，{version.document.get_source_display()}）要点摘录：{quote}", [{
                "version_id": version.pk, "document_id": version.document_id,
                "start": start, "end": start + len(quote), "hash": quote_digest(quote),
            }])
    return {"evidence": evidence, "sources": sources, "periods": periods,
            "valuation_basis": valuation_basis,
            "market_context": market_context,
            "financial_count": sum("财年截至" in item["text"] for item in evidence),
            "narrative_count": sum("摘录" in item["text"] for item in evidence),
            "problem": problem}
