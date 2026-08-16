import ipaddress
import os
import socket
import time
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


DEFAULT_TIMEOUT_SECONDS = 12
DEFAULT_MAX_BYTES = 2 * 1024 * 1024
DEFAULT_MAX_ATTEMPTS = 2
USER_AGENT = "FamilyWorkbench-Intelligence/2.0 (+private household research)"
PROXY_FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")


class SafeHttpError(Exception):
    def __init__(self, code, message, *, retryable=False):
        super().__init__(message)
        self.code = code
        self.safe_message = message
        self.retryable = retryable


@dataclass(frozen=True)
class FetchResponse:
    status: int
    url: str
    body: bytes
    etag: str = ""
    last_modified: str = ""

    @property
    def not_modified(self):
        return self.status == 304


def _safe_url_label(url):
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _allow_proxy_fake_ip(address):
    enabled = os.getenv("INTELLIGENCE_ALLOW_PROXY_FAKE_IP", "false").strip().casefold()
    return enabled in {"1", "true", "yes", "on"} and address in PROXY_FAKE_IP_NETWORK


def validate_public_http_url(url):
    parsed = urlsplit((url or "").strip())
    scheme = parsed.scheme.casefold()
    if scheme not in {"http", "https"} or not parsed.hostname:
        raise SafeHttpError("invalid_url", "信源地址必须是公开的 HTTP 或 HTTPS 地址。")
    if parsed.username or parsed.password:
        raise SafeHttpError("unsafe_url", "信源地址不能包含用户名或密码。")
    try:
        port = parsed.port
    except ValueError as exc:
        raise SafeHttpError("invalid_url", "信源地址端口无效。") from exc
    if port and port not in {80, 443}:
        raise SafeHttpError("unsafe_port", "自动采集只允许标准 HTTP/HTTPS 端口。")

    hostname = parsed.hostname.casefold().rstrip(".")
    if hostname == "localhost" or hostname.endswith((".localhost", ".local", ".internal")):
        raise SafeHttpError("private_host", "自动采集不能访问本机或内网地址。")
    try:
        literal_address = ipaddress.ip_address(hostname)
    except ValueError:
        literal_address = None
    if literal_address is not None:
        if not literal_address.is_global:
            raise SafeHttpError("private_host", "自动采集不能访问本机或内网地址。")
        return parsed.geturl()

    try:
        resolved = socket.getaddrinfo(hostname, port or (443 if scheme == "https" else 80), type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise SafeHttpError("dns_error", f"无法解析信源域名：{hostname}", retryable=True) from exc
    if not resolved:
        raise SafeHttpError("dns_error", f"无法解析信源域名：{hostname}", retryable=True)
    for entry in resolved:
        raw_address = entry[4][0].split("%", 1)[0]
        address = ipaddress.ip_address(raw_address)
        if not address.is_global and not _allow_proxy_fake_ip(address):
            raise SafeHttpError("private_host", "信源域名解析到了本机或内网地址。")
    return parsed.geturl()


class _SafeRedirectHandler(HTTPRedirectHandler):
    max_repeats = 2
    max_redirections = 4

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urljoin(req.full_url, newurl)
        validate_public_http_url(target)
        return super().redirect_request(req, fp, code, msg, headers, target)


def fetch_public_url(url, *, headers=None, timeout=DEFAULT_TIMEOUT_SECONDS, max_bytes=DEFAULT_MAX_BYTES):
    url = validate_public_http_url(url)
    request_headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/atom+xml, application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.1",
    }
    request_headers.update(headers or {})
    request = Request(url, headers=request_headers, method="GET")
    opener = build_opener(_SafeRedirectHandler())
    try:
        with opener.open(request, timeout=timeout) as response:
            final_url = response.geturl()
            validate_public_http_url(final_url)
            body = response.read(max_bytes + 1)
            if len(body) > max_bytes:
                raise SafeHttpError("response_too_large", "信源响应超过 2 MB 安全上限。")
            return FetchResponse(
                status=getattr(response, "status", 200),
                url=final_url,
                body=body,
                etag=response.headers.get("ETag", ""),
                last_modified=response.headers.get("Last-Modified", ""),
            )
    except HTTPError as exc:
        if exc.code == 304:
            return FetchResponse(status=304, url=url, body=b"")
        retryable = exc.code == 429 or 500 <= exc.code <= 599
        raise SafeHttpError(
            f"http_{exc.code}",
            f"信源返回 HTTP {exc.code}：{_safe_url_label(url)}",
            retryable=retryable,
        ) from exc
    except SafeHttpError:
        raise
    except (TimeoutError, socket.timeout) as exc:
        raise SafeHttpError("timeout", f"信源请求超时：{_safe_url_label(url)}", retryable=True) from exc
    except URLError as exc:
        reason = getattr(exc, "reason", None)
        retryable = isinstance(reason, (TimeoutError, socket.timeout, ConnectionError, OSError))
        raise SafeHttpError("network_error", f"无法连接信源：{_safe_url_label(url)}", retryable=retryable) from exc
    except OSError as exc:
        raise SafeHttpError("network_error", f"无法连接信源：{_safe_url_label(url)}", retryable=True) from exc


def fetch_with_retries(url, *, headers=None, attempts=DEFAULT_MAX_ATTEMPTS, sleep_seconds=0.4):
    attempts = max(1, min(int(attempts), 3))
    last_error = None
    for attempt in range(attempts):
        try:
            return fetch_public_url(url, headers=headers)
        except SafeHttpError as exc:
            last_error = exc
            if not exc.retryable or attempt + 1 >= attempts:
                raise
            time.sleep(sleep_seconds * (attempt + 1))
    raise last_error
