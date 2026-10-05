"""Bound public catalogue/calendar requests with a separate process deadline."""
import json
import sys
from urllib.parse import urlparse

from .fetch_worker import download, safe_failure_label

HOSTS = {"www.pbc.gov.cn", "www.mofcom.gov.cn", "www.stats.gov.cn", "www.gov.cn",
         "www.bea.gov", "www.bls.gov", "www.census.gov", "www.ismworld.org"}


def validate_page_url(url):
    parsed = urlparse(url)
    if (parsed.scheme != "https" or parsed.hostname not in HOSTS or parsed.port not in (None, 443)
            or parsed.username or parsed.password or parsed.fragment):
        raise ValueError("仅允许已登记官方目录与日历的 HTTPS 地址")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    request = json.load(sys.stdin)
    try:
        validate_page_url(request["url"])
        print(json.dumps({"text": download(request["url"])}, ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({"error": safe_failure_label(exc)}))
        sys.exit(1)
