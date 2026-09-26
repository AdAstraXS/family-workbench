"""Optional, worker-only proxy for the four reviewed public source hosts."""
import os
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .http_client import FetchResponse, SafeHttpError, USER_AGENT, fetch_public_url as direct_fetch

SOURCE_HOSTS = frozenset({
    'www.youtube.com', 'youtube.com', 'www.dwarkesh.com', 'dwarkesh.com',
    'feeds.acast.com', 'www.oaktreecapital.com', 'oaktreecapital.com',
})
NAS_PROXY = 'http://family-workbench-proxy:7890'


def source_proxy():
    value = os.environ.get('PROGRAM_SOURCE_PROXY', '').strip()
    if value and value != NAS_PROXY:
        raise SafeHttpError('proxy_config', '精选订阅代理地址与已批准的 NAS 服务不一致。')
    return value


def approved_source_url(url):
    parsed = urlsplit(url)
    try:
        valid = (parsed.scheme == 'https' and parsed.hostname in SOURCE_HOSTS
                 and parsed.port in (None, 443) and not parsed.username and not parsed.password)
    except ValueError:
        valid = False
    if not valid:
        raise SafeHttpError('proxy_source', '代理只允许访问已批准的公开信源域名。')
    return url


class SourceRedirect(HTTPRedirectHandler):
    max_redirections = 4

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = approved_source_url(urljoin(req.full_url, newurl))
        return super().redirect_request(req, fp, code, msg, headers, target)


def fetch_source_url(url, *, max_bytes=2 * 1024 * 1024, timeout=12):
    proxy = source_proxy()
    if not proxy:
        return direct_fetch(url, max_bytes=max_bytes, timeout=timeout)
    # DNS for these fixed HTTPS hosts is resolved by the proxy. Local DNS on the
    # NAS may return poisoned addresses; arbitrary caller-supplied hosts never
    # use this path. Every redirect is checked against the same fixed catalogue.
    request = Request(approved_source_url(url), headers={'User-Agent': USER_AGENT})
    opener = build_opener(ProxyHandler({'https': proxy}), SourceRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            final_url = approved_source_url(response.geturl())
            body = response.read(max_bytes + 1)
            if len(body) > max_bytes:
                raise SafeHttpError('response_too_large', '信源正文超过读取上限。')
            return FetchResponse(status=response.status, url=final_url, body=body)
    except SafeHttpError:
        raise
    except Exception as exc:
        raise SafeHttpError('proxy_network', '信源代理连接失败，请检查 NAS 代理和订阅状态。', retryable=True) from exc
