"""Bounded, verified HTTPS fetching for the issuer's known official sources."""
import gzip
import io
import json
import re
import time
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlsplit, urlunsplit


class IRError(ValueError):
    def __init__(self, message, *, status=None):
        super().__init__(message)
        self.status = status


def official_url(company, value, base=None):
    raw = str(value or '').strip()
    if not raw or len(raw) > 2000 or any(ord(c) < 32 for c in raw) or '\\' in raw:
        raise IRError('官方资料链接格式无效。')
    try:
        parts = urlsplit(urljoin(base or company.entry, raw))
        port = parts.port
    except ValueError as exc:
        raise IRError('官方资料链接端口无效。') from exc
    if parts.scheme not in ('http', 'https') or parts.username or parts.password or port not in (None, 443):
        raise IRError('仅允许官方 HTTPS 资料链接。')
    host = parts.hostname
    path = unquote(unquote(parts.path))
    if '\\' in path or any(ord(c) < 32 for c in path) or '..' in path.split('/'):
        raise IRError('官方资料路径无效。')
    allowed = host == urlsplit(company.entry).hostname
    for asset_host, prefix in company.assets:
        if host != asset_host:
            continue
        if prefix.startswith('/@issuer/'):
            issuer = prefix.split('/')[2]
            allowed |= bool(re.match(r'^/_[a-f0-9]+/' + issuer + r'/db/', path))
        else:
            allowed |= path.startswith(prefix)
    if not allowed:
        raise IRError('链接不属于本公司的已核实官方来源。')
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                       if not k.lower().startswith('utm_') and k.lower() not in ('gclid', 'fbclid')])
    normalized_path = parts.path or '/'
    if host == 'www.microsoft.com':
        normalized_path = normalized_path.lower()
    url = urlunsplit(('https', host, normalized_path, query, ''))
    if len(url) > 1000:
        raise IRError('官方链接过长，未自动归档。')
    return url


class _Redirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, company):
        self.company = company

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return super().redirect_request(req, fp, code, msg, headers,
                                        official_url(self.company, newurl, req.full_url))


@dataclass
class IRResponse:
    url: str
    raw: bytes
    content_type: str

    @property
    def html(self):
        # Beautiful Soup handles declared encodings when handed raw bytes.
        return self.raw

    def json(self):
        try:
            return json.loads(self.raw.decode('utf-8-sig'))
        except (ValueError, UnicodeError) as exc:
            raise IRError('官方列表没有返回有效 JSON。') from exc


class IRClient:
    def __init__(self, company, *, timeout=15, max_bytes=20*1024*1024, opener=None, interval=.5):
        self.company = company
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.opener = opener or urllib.request.build_opener(_Redirect(company))
        self.interval = interval
        self.last_request = 0
        self.cache = {}

    def get(self, url):
        url = official_url(self.company, url)
        if url in self.cache:
            return self.cache[url]
        time.sleep(max(0, self.interval - (time.monotonic() - self.last_request)))
        self.last_request = time.monotonic()
        request = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0 (compatible; FamilyWorkbench-Research/1.0)',
            'Accept': 'text/html,application/pdf,application/json,application/xml;q=0.9,*/*;q=0.5',
            'Accept-Encoding': 'gzip, deflate',
        })
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                final = official_url(self.company, response.geturl())
                raw = response.read(self.max_bytes + 1)
                if len(raw) > self.max_bytes:
                    raise IRError('官方资料超过单份大小限制。')
                encoding = response.headers.get('Content-Encoding', '').lower()
                if encoding == 'gzip':
                    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as compressed:
                        raw = compressed.read(self.max_bytes + 1)
                elif encoding == 'deflate':
                    decoder = zlib.decompressobj()
                    raw = decoder.decompress(raw, self.max_bytes + 1)
                    if decoder.unconsumed_tail:
                        raise IRError('官方资料解压后超出大小限制。')
                elif encoding not in ('', 'identity'):
                    raise IRError('官方资料返回了不支持的压缩格式。')
                if len(raw) > self.max_bytes:
                    raise IRError('官方资料解压后超出大小限制。')
                result = IRResponse(final, raw, response.headers.get('Content-Type', '').split(';')[0])
        except urllib.error.HTTPError as exc:
            raise IRError(f'官方站点返回 HTTP {exc.code}；已有资料已保留。', status=exc.code) from exc
        except (OSError, ValueError, EOFError, zlib.error) as exc:
            if isinstance(exc, IRError):
                raise
            raise IRError('官方站点连接、证书或响应读取失败；请稍后重试。') from exc
        self.cache[url] = result
        return result
