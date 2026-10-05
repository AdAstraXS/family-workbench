"""One bounded subprocess per request: third-party libraries cannot hang the job."""
import contextlib
from http.cookiejar import CookieJar
import io
import json
import os
import socket
import sys
from urllib.parse import urlparse
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, HTTPCookieProcessor, ProxyHandler, Request, build_opener

from .registry import GROUPS, OFFICIAL_GROUPS

OFFICIAL_HOSTS = {"pbc": "www.pbc.gov.cn", "mofcom": "www.mofcom.gov.cn", "nbs_release": "www.stats.gov.cn",
                  "ism_manufacturing": "www.ismworld.org", "ism_services": "www.ismworld.org", "gov_budget": "www.gov.cn"}


def validate_url(url, group):
    parsed = urlparse(url)
    if (parsed.scheme != "https" or parsed.hostname != OFFICIAL_HOSTS.get(group)
            or parsed.port not in (None, 443) or parsed.username or parsed.password or parsed.fragment):
        raise ValueError("仅允许对应官方站点的 HTTPS 发布稿")


class SameHostRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlparse(newurl).netloc != urlparse(req.full_url).netloc or urlparse(newurl).scheme != "https":
            raise ValueError("不允许跨站重定向")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class ISMPublicSession(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old, new = urlparse(req.full_url), urlparse(newurl)
        bridge = ((old.hostname == "www.ismworld.org" and new.hostname == "ecommerce.ismworld.org" and new.path == "/SSO/Login.aspx")
                  or (old.hostname == "ecommerce.ismworld.org" and new.hostname == "www.ismworld.org"))
        if (new.scheme != "https" or new.port not in (None, 443) or new.username or new.password
                or new.hostname not in {"www.ismworld.org", "ecommerce.ismworld.org"}
                or (new.hostname != old.hostname and not bridge)):
            raise ValueError("ISM会话跳转地址未登记")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(url):
    headers = {"User-Agent": "Mozilla/5.0 FamilyWorkbenchMacro/1.0 (+https://adastrax.top/macro/sources/)",
               "Accept": "text/html,text/calendar,text/csv,application/json;q=0.9,*/*;q=0.5"}
    request = Request(url, headers=headers)
    is_ism = urlparse(url).hostname == "www.ismworld.org"
    handlers = [ISMPublicSession(), HTTPCookieProcessor(CookieJar())] if is_ism else [SameHostRedirect()]
    opener = build_opener(*handlers)
    if is_ism and not url.endswith("/reports/ism-pmi-reports/"):
        # Public report pages share the anonymous session established by their
        # index. Initialising it avoids the site's first-visit SSO/404 response.
        with opener.open(Request("https://www.ismworld.org/supply-management-news-and-reports/reports/ism-pmi-reports/", headers=headers), timeout=25):
            pass
    try:
        response = opener.open(request, timeout=25)
    except HTTPError as exc:
        # Reuse the existing NAS outbound proxy only for public US sources.
        # Do not change proxy rules, disable TLS, or route household data.
        if exc.code != 403 or urlparse(url).hostname not in {"www.bls.gov", "www.census.gov", "www.ismworld.org"}:
            raise
        proxy = os.environ.get("MACRO_SOURCE_PROXY", "")
        if not proxy:
            try:
                socket.gethostbyname("family-workbench-proxy")
                proxy = "http://family-workbench-proxy:7890"
            except OSError:
                raise exc
        opener = build_opener(ProxyHandler({"https": proxy}), *handlers)
        response = opener.open(request, timeout=25)
    if is_ism and urlparse(response.url).hostname != "www.ismworld.org":
        # The public site may first initialise an anonymous session. No account
        # credentials are supplied, stored or scraped from an interactive form.
        response.close()
        response = opener.open(request, timeout=25)
        if urlparse(response.url).hostname != "www.ismworld.org":
            response.close()
            raise ValueError("ISM需要交互登录，保留原数据")
    with response:
        raw = response.read(20_000_001)
        if len(raw) > 20_000_000:
            raise ValueError("响应超过大小限制")
        return raw.decode("utf-8-sig")


def fetch(group, url=""):
    spec = GROUPS[group][0]
    if spec.provider == "fred":
        url = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=" + spec.field
        return {"url": url, "text": download(url)}
    if group in OFFICIAL_GROUPS:
        validate_url(url, group)
        return {"url": url, "text": download(url)}
    # Only allow functions and parameters from the reviewed dictionary, not user-supplied names.
    with contextlib.redirect_stdout(io.StringIO()):
        import akshare as ak
        params = dict(spec.params)
        function = params.pop("function")
        frame = getattr(ak, function)(**params)
        if spec.provider in {"nbs", "nbs_city"}:
            frame = frame.rename_axis("index").reset_index()
        # AKShare delivers numeric pandas columns. Convert via decimal text, never calculate with floats.
        rows = [{str(key): None if ak_value is None or str(ak_value) in {"nan", "NaT", "<NA>"}
                 else str(ak_value) for key, ak_value in row.items()} for row in frame.to_dict("records")]
    return {"url": "https://data.stats.gov.cn/" if spec.provider.startswith("nbs") else "https://data.eastmoney.com/cjsj/",
            "rows": rows, "library": "akshare", "library_version": ak.__version__}


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    request = json.load(sys.stdin)
    try:
        result = fetch(request["group"], request.get("url", ""))
        print(json.dumps(result, ensure_ascii=False))
    except Exception as exc:
        # Do not leak proxy credentials, environment values or HTML error bodies.
        print(json.dumps({"error": type(exc).__name__}))
        sys.exit(1)
