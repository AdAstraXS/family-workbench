"""Bounded public-network requests; pin DNS and validate all redirect targets."""
import http.client
import ipaddress
import socket
import ssl
import time
from urllib.parse import urlsplit, urlunsplit, urljoin


class WebCaptureError(ValueError):
    pass


def canonical_url(value):
    value = str(value).strip()
    if len(value) > 1000 or any(ord(c) <= 32 or ord(c) == 127 for c in value) or "\\" in value:
        raise WebCaptureError("链接格式错误或超过 1000 字符。")
    try:
        p = urlsplit(value)
        host = (p.hostname or "").encode("idna").decode().lower().rstrip(".")
        port = p.port
    except (ValueError, UnicodeError) as exc:
        raise WebCaptureError("请填写有效的公开网页链接。") from exc
    if p.scheme not in {"http", "https"} or not host or p.username or p.password:
        raise WebCaptureError("仅支持无内嵌凭据的 HTTP / HTTPS 网页。")
    if port not in {None, 80 if p.scheme == "http" else 443}:
        raise WebCaptureError("只支持标准网页端口。")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and (not address.is_global or address.is_multicast or address.is_reserved):
        raise WebCaptureError("不能访问本机或内网地址。")
    if address is None and (host == "localhost" or "." not in host or host.endswith((".local", ".internal", ".localhost"))):
        raise WebCaptureError("不能访问本机或内网地址。")
    return urlunsplit((p.scheme, f"[{host}]" if ":" in host else host, p.path or "/", p.query, ""))


def public_addresses(url):
    p = urlsplit(canonical_url(url))
    try:
        addresses = list(dict.fromkeys(i[4][0] for i in socket.getaddrinfo(p.hostname, 443 if p.scheme == "https" else 80, type=socket.SOCK_STREAM)))
    except OSError as exc:
        raise WebCaptureError("网页域名暂时无法解析。") from exc
    if not addresses or any(not ipaddress.ip_address(a).is_global or ipaddress.ip_address(a).is_multicast or ipaddress.ip_address(a).is_reserved for a in addresses):
        raise WebCaptureError("网页或图片指向内网，已停止访问。")
    return addresses


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, address, timeout):
        super().__init__(host, timeout=timeout, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        sock = socket.create_connection((self.address, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except Exception:
            sock.close()
            raise


def public_request(url, *, limit, timeout=20, data=None, headers=None, redirects=3):
    deadline = time.monotonic() + timeout
    for hop in range(redirects + 1):
        url = canonical_url(url)
        p = urlsplit(url)
        address = public_addresses(url)[0]
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise WebCaptureError("网页服务响应超时。")
        connection = PinnedHTTPSConnection(p.hostname, address, remaining) if p.scheme == "https" else http.client.HTTPConnection(address, port=80, timeout=remaining)
        try:
            connection.request("POST" if data is not None else "GET", urlunsplit(("", "", p.path, p.query, "")), body=data,
                headers={"Host": p.netloc, "User-Agent": "FamilyWorkbenchKnowledge/1.0", "Accept-Encoding": "identity", **(headers or {})})
            response = connection.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                if data is not None or hop == redirects or not response.getheader("Location"):
                    raise WebCaptureError("网页服务重定向异常。")
                url = urljoin(url, response.getheader("Location"))
                continue
            if response.status != 200:
                raise WebCaptureError(f"网页服务返回 HTTP {response.status}，请稍后重试。")
            if response.getheader("Content-Encoding", "identity") not in {"", "identity"}:
                raise WebCaptureError("网页服务返回不支持的压缩内容。")
            body = bytearray()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WebCaptureError("网页服务响应超时。")
                if connection.sock:
                    connection.sock.settimeout(remaining)
                chunk = response.read1(min(65536, limit + 1 - len(body)))
                if not chunk:
                    break
                body.extend(chunk)
                if len(body) > limit:
                    raise WebCaptureError("网页或图片超过大小限制。")
            return bytes(body), response.getheader("Content-Type", "application/octet-stream")
        except (OSError, http.client.HTTPException) as exc:
            raise WebCaptureError("网页服务暂时无法访问，请稍后重试。") from exc
        finally:
            connection.close()
    raise WebCaptureError("网页重定向次数过多。")
