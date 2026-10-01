"""US and foreign issuer filings, exhibits and taxonomy-neutral XBRL facts."""
import re
import hashlib
from collections import defaultdict
from django.conf import settings
from .company_identity import identity_for
from .material_store import save_material, record_failure
from .providers.sec import filing_url, SUBMISSIONS_URL_TEMPLATE, parse_recent_filings
from .sec_content import extract_sec_html
from .source_sync import _default_sec_client


def resolve_sec(security, client):
    identity = identity_for(security)
    if not identity.cik:
        if not identity.sec_ticker:
            raise ValueError("该上市市场没有已确认的 SEC 申报身份；请使用富途及公司官方 IR 资料。")
        identity.cik = client.resolve_cik(identity.sec_ticker)
        identity.save(update_fields=["cik", "updated_at"])
    return identity.cik


def discover(security, client=None):
    client = client or _default_sec_client(security)
    cik = resolve_sec(security, client)
    url = SUBMISSIONS_URL_TEMPLATE.format(cik=cik)
    payload = client.get_json(url)
    records = parse_recent_filings(payload, company_name=payload.get("name", ""))
    # Always include annual reports even when recent 6-K announcements are numerous.
    chosen, counts = [], defaultdict(int)
    for record in records:
        base = record["document_type"]
        limit = 3 if base in {"10-k", "20-f", "40-f"} else 4 if base == "10-q" else 3
        if counts[base] < limit:
            chosen.append(record)
            counts[base] += 1
    serializable = [{**r, "filing_date": r["filing_date"].isoformat(),
                    "report_date": r["report_date"].isoformat() if r["report_date"] else ""} for r in chosen]
    save_material(security, "sec", "sec_index", "SEC 文件目录", source_url=url,
        data={"cik": cik, "company_name": payload.get("name"), "filings": serializable,
              "historical_files": payload.get("filings", {}).get("files", []), "original": payload})
    if not chosen:
        raise ValueError("SEC 返回了公司目录，但没有支持类型的报告；未取得正文。")
    return cik, serializable


def download(security, cik, record, client=None):
    client = client or _default_sec_client(security)
    url = filing_url(cik, record["accession"], record["primary_document"])
    key = "sec:" + record["accession"]
    _document(security, key, url, record["title"], record, client)
    # index.json discovers exhibits even when the cover contains no links.
    directory_url = url.rsplit("/", 1)[0] + "/index.json"
    directory = client.get_json(directory_url)
    save_material(security, key + ":index", "sec_index", record["title"] + " · 附件目录",
                  source_url=directory_url, data=directory)
    candidates = []
    for item in directory.get("directory", {}).get("item", []):
        name = str(item.get("name", ""))
        if name == record["primary_document"] or not re.fullmatch(r"[A-Za-z0-9_.-]+\.(?:htm|html|pdf)", name, re.I):
            continue
        # Skip XBRL rendered tables; select narrative exhibits/report attachments.
        if re.fullmatch(r"R\d+\.html?", name, re.I) or name.endswith("-index.html"):
            continue
        candidates.append(name)
    if len(candidates) > 12:
        candidates = sorted(candidates, key=lambda n: not bool(re.search(r"99|ex|report|financial|release", n, re.I)))
    failures = []
    from .models import CompanyMaterial
    for position, name in enumerate(candidates[:100]):
        attachment_url = filing_url(cik, record["accession"], name)
        attachment_key = key + ":" + hashlib.sha256(name.encode()).hexdigest()[:24]
        CompanyMaterial.objects.get_or_create(security=security, key=attachment_key,
            defaults={"kind": "sec_document", "title": record["title"] + " · " + name,
                      "source_url": attachment_url, "metadata": {**record, "attachment": name, "cik": cik}})
        if position >= 12:
            continue
        try:
            _document(security, attachment_key[:180], attachment_url, record["title"] + " · " + name,
                      {**record, "attachment": name}, client)
        except Exception:
            record_failure(security, attachment_key[:180], "sec_document", name, "附件未能获取或提取，请单独重试。")
            failures.append(name)
    if failures:
        raise ValueError(f"主文件已保存，{len(failures)} 份附件未完成。")
    return "主文件与附件已保存" + (f"；另有 {len(candidates) - 12} 份附件待按需获取" if len(candidates) > 12 else "")


def _document(security, key, url, title, record, client):
    raw = client.get_document_html(url, max_bytes=settings.RESEARCH_SEC_DOCUMENT_MAX_BYTES)
    media_type = "application/pdf" if url.lower().endswith(".pdf") else "text/html"
    warning = ""
    try:
        if media_type == "application/pdf":
            from .ir_extraction import extract_material
            from .providers.ir_http import IRResponse
            text = extract_material(IRResponse(url, raw, media_type))["text"]
        else:
            text = extract_sec_html(raw)
    except Exception:
        text, warning = "", "原件已保存；尚未取得可读正文，可能是图片或超出提取限额。"
    save_material(security, key, "sec_document", title, source_url=url, raw=raw,
        media_type=media_type, text=text, data={**record, "warning": warning},
        report_date=record.get("report_date") or record.get("filing_date"))


def company_facts(security, client=None):
    client = client or _default_sec_client(security)
    cik = resolve_sec(security, client)
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
    data = client.get_json(url)
    if not isinstance(data.get("facts"), dict) or not data["facts"]:
        raise ValueError("SEC 没有返回可用的结构化财务指标。")
    save_material(security, "facts", "facts", "SEC 财务指标（原币种与会计准则）", source_url=url, data=data)
    return "已保存 US GAAP / IFRS 原始指标，保留每条数据单位和申报来源"
