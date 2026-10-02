"""Company data, never company-specific branches in the capture pipeline."""
import re
from urllib.parse import urlsplit
from .catalogue import contains
from .models import WatchRule
from .services import WatchError


def domains(values):
    if not isinstance(values, list) or len(values) > 20:
        raise WatchError("官方域名最多 20 个。")
    result = []
    for value in values:
        if not isinstance(value, str):
            raise WatchError("官方域名格式无效。")
        value = value.strip().lower().rstrip(".")
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", value) or "." not in value or ".." in value:
            raise WatchError("官方域名只填写域名，如 microsoft.com，不包含路径。")
        result.append(value)
    return list(dict.fromkeys(result))


_UNSET = object()


def profile(dossier, rule=_UNSET):
    if rule is _UNSET:
        rule = WatchRule.objects.filter(dossier=dossier).first()
    return {"company": dossier.security.name, "symbol": dossier.security.symbol,
            "aliases": rule.aliases if rule else [dossier.security.name, dossier.security.symbol],
            "products": rule.products if rule else [], "topics": rule.topics if rule else [],
            "official_domains": rule.official_domains if rule else [],
            "rule_version": rule.version if rule else 0,
            "business_context": rule.business_context if rule else ""}


def official(version, company):
    host = (urlsplit(version.url).hostname or "").casefold()
    return bool(version.official_version_id or any(host == d or host.endswith("." + d) for d in company["official_domains"]))


def relevance(version, company):
    text = version.title + " " + version.summary
    if any(contains(text, w) for w in company["aliases"] + company["products"]):
        return "direct"
    if any(contains(text, w) for w in company["topics"]):
        return "industry"
    return "unknown"
