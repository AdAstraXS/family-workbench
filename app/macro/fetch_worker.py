"""One bounded subprocess per request: third-party libraries cannot hang the job."""
import contextlib
import io
import json
import sys
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .registry import GROUPS, OFFICIAL_GROUPS

OFFICIAL_HOSTS = {"pbc": "www.pbc.gov.cn", "mofcom": "www.mofcom.gov.cn", "nbs_release": "www.stats.gov.cn"}


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


def download(url):
    with build_opener(SameHostRedirect()).open(Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=25) as response:
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
