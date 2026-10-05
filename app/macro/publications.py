"""Published reports prove periods/dates; calendar plans never do."""
import re
from datetime import date
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from django.db import transaction
from django.utils import timezone

from .adapters import SourceError, digest, parse_official
from .models import MacroImportRun, MacroObservation, MacroObservationRevision, MacroPublication, MacroSourceMapping
from .services import fetch_page, import_group
from .registry import GROUPS

ISM_INDEX = "https://www.ismworld.org/supply-management-news-and-reports/reports/ism-pmi-reports/"
BLS_RELEASES = {
    "employment": ("https://www.bls.gov/news.release/empsit.nr0.htm", ["PAYEMS", "UNRATE", "CIVPART", "CES0500000003"]),
    "cpi": ("https://www.bls.gov/news.release/cpi.nr0.htm", ["CPIAUCSL", "CPILFESL", "CPIAUCNS", "CPILFENS"]),
    "ppi": ("https://www.bls.gov/news.release/ppi.nr0.htm", ["PPIFIS", "PPIFID"]),
}
MONTHS = {name.lower(): i for i, name in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"], 1)}


def bls_publication(text, key, url, codes):
    soup = BeautifulSoup(text, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    period = re.search(r"(20\d{2})\s+M(\d{2})\s+Results", title)
    pre = soup.find("pre")
    header = pre.get_text(" ", strip=True)[:450] if pre else ""
    stamp = re.search(r"(" + "|".join(MONTHS) + r")\s+(\d{1,2}),\s+(20\d{2})", header, re.I)
    if not period or not stamp or "embargoed until" not in header.lower():
        raise SourceError("BLS发布稿统计期或实际发布时间未核验")
    return {"agency": "bls", "country": "US", "key": key, "title": title,
            "period_date": date(int(period[1]), int(period[2]), 1),
            "release_date": date(int(stamp[3]), MONTHS[stamp[1].lower()], int(stamp[2])),
            "codes": codes, "source_url": url, "source_hash": digest(text), "evidence": {"header": header}}


def store_publication(item):
    today = timezone.localdate()
    if item["period_date"] > today or (item["release_date"] and not item["period_date"] <= item["release_date"] <= today):
        raise SourceError("发布稿含未来或不合理日期，保留原记录")
    obj, _ = MacroPublication.objects.update_or_create(
        agency=item["agency"], key=item["key"], period_date=item["period_date"],
        defaults={**item, "verified_at": timezone.now()})
    return obj


def refresh_publications(*, write=False, reader=fetch_page):
    result = {"sources": [], "failures": []}
    for key, (url, codes) in BLS_RELEASES.items():
        try:
            item = bls_publication(reader(url), key, url, codes)
            if write:
                store_publication(item)
            result["sources"].append({"key": key, "period": str(item["period_date"]), "date": str(item["release_date"])})
        except Exception as exc:
            result["failures"].append({"group": "publication_bls_" + key,
                "error": str(exc) if isinstance(exc, SourceError) else "发布稿读取失败；保留原日期"})
    if write:
        result["dates"] = annotate_publications()
    return result


def latest_ism_links(text):
    result = {}
    for anchor in BeautifulSoup(text, "html.parser").find_all("a", href=True):
        if anchor.get_text(" ", strip=True) != "View Report":
            continue
        url = urljoin(ISM_INDEX, anchor["href"])
        parsed = urlparse(url)
        if parsed.hostname != "www.ismworld.org" or parsed.scheme != "https" or parsed.username or parsed.password:
            raise SourceError("ISM目录报告地址异常")
        for group, segment in [("ism_manufacturing", "pmi"), ("ism_services", "services")]:
            if re.fullmatch(r"/supply-management-news-and-reports/reports/ism-pmi-reports/" + segment + r"/[a-z]+/", parsed.path):
                if group in result and result[group] != url:
                    raise SourceError("ISM当前报告地址重复，需核验目录")
                result[group] = url
    if len(result) != 2:
        raise SourceError("ISM当前制造业或服务业报告链接缺失")
    return result


def update_ism(*, write=False, reader=fetch_page):
    result = {"reports": [], "failures": []}
    try:
        links = latest_ism_links(reader(ISM_INDEX))
    except Exception as exc:
        result["failures"].append({"group": "ism_discovery", "error": str(exc) if isinstance(exc, SourceError) else "ISM目录请求失败；保留原数据"})
        return result
    for group, url in links.items():
        try:
            text = reader(url)
            point = parse_official(text, group)[0]
            summary = import_group(group, write=write, url=url, fetcher=lambda *_: {"url": url, "text": text})
            if write:
                store_publication({"agency": "ism", "country": "US", "key": group, "title": GROUPS[group][0].name,
                    "period_date": point.period, "release_date": point.release_date, "codes": [point.code],
                    "source_url": url, "source_hash": digest(text), "evidence": point.evidence})
            result["reports"].append({"group": group, "url": url, **summary})
        except Exception as exc:
            result["failures"].append({"group": group, "error": str(exc) if isinstance(exc, SourceError) else "ISM报告读取失败；保留原数据"})
    return result


def verified_release_dates(mapping):
    result = {}
    for published in MacroPublication.objects.filter(country=mapping.indicator.country, release_date__isnull=False).order_by("verified_at"):
        if mapping.indicator.code in published.codes:
            result[published.period_date] = published
    return result


@transaction.atomic
def annotate_publications():
    run = None
    revised = 0
    proofs = {}
    for published in MacroPublication.objects.filter(country="US", release_date__isnull=False).order_by("verified_at"):
        for code in published.codes:
            proofs[(code, published.period_date)] = published
    if not proofs:
        return {"revised": 0}
    mappings = list(MacroSourceMapping.objects.filter(indicator__country="US",
        indicator__code__in={code for code, _ in proofs}).order_by("indicator__code").select_for_update())
    for observation in MacroObservation.objects.select_related("mapping__indicator").filter(
            mapping__in=mappings, period_date__in={period for _, period in proofs}).order_by("mapping_id", "pk").select_for_update(of=("self",)):
        publication = proofs.get((observation.mapping.indicator.code, observation.period_date))
        if not publication or observation.release_date == publication.release_date:
            continue
        if observation.release_date and observation.release_date != publication.release_date:
            raise SourceError("已有实际发布日期与发布稿冲突，请核验")
        prior = observation.revisions.order_by("-number").first()
        if not prior:
            raise SourceError("发布日期补充缺少原值证据")
        if run is None:
            run = MacroImportRun.objects.create(group="release_dates")
        observation.release_date = publication.release_date
        observation.revision += 1
        observation.fingerprint = digest({"value": str(observation.value), "release": str(publication.release_date),
            "definition": observation.mapping.definition_hash, "notes": prior.evidence.get("row", {}).get("source_notes", "")})
        observation.save(update_fields=["release_date", "revision", "fingerprint"])
        MacroObservationRevision.objects.create(observation=observation, run=run, number=observation.revision,
            value=observation.value, release_date=publication.release_date, source_url=prior.source_url, source_hash=prior.source_hash,
            evidence={**prior.evidence, "publication": {"url": publication.source_url, "hash": publication.source_hash,
                                                       "evidence": publication.evidence}})
        revised += 1
    if run:
        run.status, run.finished_at, run.summary = "success", timezone.now(), {"revised": revised, "created": 0, "missing": 0,
            "latest_period": str(max(period for _, period in proofs))}
        run.save()
    return {"revised": revised}
