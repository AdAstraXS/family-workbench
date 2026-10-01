"""Explicit company selection, market-qualified listings and audited aliases."""
import re
from django.core import signing
from django.db import transaction
from django.db.models import Q
from portfolio.models import Security
from .models import CompanyIdentity, ResearchDossier
from .providers.ir_registry import COMPANIES
from .services import _require_writer, ResearchValidationError

# Aliases identify the issuer, not an ADR conversion ratio or accounting currency.
GROUPS = (
    ("NIO 蔚来", ("US.NIO", "HK.09866", "SG.NIO"), "NIO"),
    ("Alibaba 阿里巴巴", ("US.BABA", "HK.09988"), "BABA"),
    ("TSMC 台积电", ("US.TSM", "TW.2330"), "TSM"),
    ("ASML 阿斯麦", ("US.ASML", "NL.ASML"), "ASML"),
    ("BYD 比亚迪", ("SZ.002594", "HK.01211", "US.BYDDY"), ""),
    ("Tencent 腾讯", ("HK.00700", "US.TCEHY"), ""),
    ("SK hynix SK海力士 海力士", ("KR.000660",), ""),
)


def qualified(security):
    market = security.exchange if security.market == "CN" else security.market
    return f"{market}.{security.symbol}"


def relation(code):
    for name, listings, ticker in GROUPS:
        if code in listings:
            return {"name": name, "listings": list(listings), "sec_ticker": ticker}
    for company in COMPANIES:
        listings = [f"{m}.{s}" for m, s in company.identities]
        if code in listings:
            return {"name": company.name, "listings": listings,
                    "sec_ticker": company.symbol if company.market == "US" else ""}
    return {"listings": [code], "sec_ticker": code.split(".", 1)[1] if code.startswith("US.") else ""}


def _candidate(code, name, source):
    if not re.fullmatch(r"(?:US|HK|SH|SZ|TW|KR|SG|NL)\.[A-Za-z0-9.-]{1,20}", code):
        return None
    payload = {"code": code, "name": str(name)[:200], "source": source}
    return {**payload, **relation(code), "name": str(name)[:200],
            "token": signing.dumps(payload, salt="company-choice")}


def search_companies(query, *, searcher=None):
    query = str(query or "").strip()[:80]
    if len(query) < 2:
        raise ResearchValidationError("请输入至少两个字符的公司名称或代码。")
    results = {}
    def add(code, name, source):
        candidate = _candidate(code, name, source)
        if candidate:
            results.setdefault(code, candidate)
    for name, listings, _ in GROUPS:
        if query.casefold() in (name + " " + " ".join(listings)).casefold():
            for code in listings:
                add(code, name, "已核对的多地上市关系")
    for company in COMPANIES:
        if query.casefold() in (company.name + " " + company.symbol).casefold():
            add(f"{company.market}.{company.symbol}", company.name, "官方 IR 目录")
    for security in Security.objects.filter(asset_type="stock").filter(
            Q(name__icontains=query) | Q(symbol__iexact=query))[:20]:
        add(qualified(security), security.name, "本地证券目录")
    from .company_sources import search_futu
    error = ""
    try:
        for row in (searcher or search_futu)(query):
            if row.get("sec_type") in (3, "STOCK", "Stock", "STOCK_TYPE_STOCK"):
                add(row["code"], row["name"], "富途股票搜索")
    except Exception:
        error = "富途搜索暂不可用，以下仅显示本地已识别的公司。"
    return list(results.values())[:30], error


def choose_company(actor, token):
    _require_writer(actor)
    try:
        choice = signing.loads(token, salt="company-choice", max_age=3600)
        prefix, symbol = choice["code"].split(".", 1)
    except (signing.BadSignature, KeyError, ValueError, TypeError) as exc:
        raise ResearchValidationError("公司选择已失效，请重新搜索。") from exc
    market = "CN" if prefix in {"SH", "SZ"} else prefix
    with transaction.atomic():
        security, _ = Security.objects.get_or_create(market=market, symbol=symbol,
            defaults={"name": choice["name"], "asset_type": "stock", "exchange": prefix,
                      "currency": {"US": "USD", "HK": "HKD", "CN": "CNY", "TW": "TWD", "KR": "KRW", "SG": "SGD", "NL": "EUR"}[market]})
        if security.asset_type != "stock":
            raise ResearchValidationError("这个代码已有非股票记录，请核对证券身份。")
        info = relation(choice["code"])
        CompanyIdentity.objects.get_or_create(security=security, defaults={
            "name": info.get("name", choice["name"]), "aliases": [choice["name"]],
            "listings": info["listings"], "sec_ticker": info["sec_ticker"],
            "provenance": {"selection_source": choice["source"], "relations": "已核对目录；其余证券仅记录自身"}})
        dossier, _ = ResearchDossier.objects.get_or_create(owner=actor, security=security,
            defaults={"family": actor.family, "initial_thesis": ""})
    return dossier


def identity_for(security):
    info = relation(qualified(security))
    identity, _ = CompanyIdentity.objects.get_or_create(security=security, defaults={
        "name": info.get("name", security.name), "listings": info["listings"],
        "sec_ticker": info["sec_ticker"], "provenance": {"source": "已有证券及核对目录"}})
    return identity
