"""Microsoft Investor Relations 专用 connector（M2A-3）。

MSFT 专用，非通用公司官网爬虫：
- 入口固定 https://www.microsoft.com/en-us/investor/；
- 只允许 OFFICIAL_HOSTS 内的 HTTPS 官方 host，拒绝 userinfo 与非默认端口；
- 首页只识别官方财报页模式 /en-us/investor/earnings/fy-YYYY-qN/press-release-webcast，
  无法可靠分类的链接一律忽略；
- 正文纯文本提取（剥 script/style/nav 等，保留段落/列表/表格行），有字符上限；
- 页面异常、空正文、超限、外站跳转均显式失败，由同步层保留旧数据。

只用 stdlib（urllib + html.parser），opener 可注入，离线测试全部 mock。
"""
import math
import re
import urllib.error
import urllib.request
from datetime import date, datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit

OFFICIAL_HOSTS = frozenset({"www.microsoft.com", "microsoft.com"})
HOME_URL = "https://www.microsoft.com/en-us/investor/"
DEFAULT_USER_AGENT = "FamilyWorkbench-Research/1.0 (MSFT IR sync)"
DEFAULT_TIMEOUT_SECONDS = 15
DEFAULT_MAX_RESPONSE_BYTES = 5 * 1024 * 1024
DEFAULT_MAX_CONTENT_CHARS = 200_000
EXTERNAL_ID_MAX_LENGTH = 255
TITLE_MAX_LENGTH = 500

# 官方财报页固定模式（press-release-webcast 是发布页，不当作 transcript）
EARNINGS_PATH_PATTERN = re.compile(
    r"^/en-us/investor/earnings/fy-(\d{4})-q([1-4])/press-release-webcast/?$",
    re.IGNORECASE,
)
RELEASE_DATELINE_PATTERN = re.compile(
    r"\bREDMOND,\s*Wash\.\s*[—–-]\s*"
    r"(January|February|March|April|May|June|July|August|September|October|"
    r"November|December)\s+(\d{1,2}),\s+(\d{4})\s*[—–-]",
    re.IGNORECASE,
)

SKIP_TAGS = frozenset(
    {"head", "script", "style", "nav", "noscript", "iframe", "svg", "template"}
)
BLOCK_TAGS = frozenset(
    {
        "p", "div", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6",
        "br", "hr", "tr", "table", "section", "article", "main", "footer",
        "header", "blockquote", "pre", "figure", "figcaption", "dt", "dd",
    }
)


class MicrosoftIRError(Exception):
    """Microsoft IR connector 所有错误的基类。"""


class MicrosoftIRConfigError(MicrosoftIRError):
    """配置缺失/非法（如空 UA）。"""


class MicrosoftIRUrlError(MicrosoftIRError):
    """URL 规范化/host 白名单校验失败。"""


class MicrosoftIRHTTPError(MicrosoftIRError):
    """官方站点返回非 2xx 状态码。"""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class MicrosoftIRNetworkError(MicrosoftIRError):
    """连接失败/超时前的网络层错误。"""


class MicrosoftIRTimeoutError(MicrosoftIRError):
    """请求超时。"""


class MicrosoftIRResponseTooLarge(MicrosoftIRError):
    """响应超过最大字节数。"""


class MicrosoftIRParseError(MicrosoftIRError):
    """页面结构/内容解析失败（含空正文、超限）。"""


class MicrosoftIREmptyBodyError(MicrosoftIRParseError):
    """正文为空——空正文不算成功抓取。"""


def normalize_url(url, base=HOME_URL):
    """规范化 URL：相对路径按 base 解析；HTTPS + 官方 host；去 query/fragment。

    拒绝：非 HTTPS、非微软官方 host、userinfo、非默认端口（443）、
    控制字符、超过 255 字符。失败抛 MicrosoftIRUrlError。
    """
    raw = str(url or "").strip()
    if not raw:
        raise MicrosoftIRUrlError("URL 为空。")
    if any(ord(ch) < 32 for ch in raw):
        raise MicrosoftIRUrlError(f"URL 含控制字符：{raw[:40]!r}")
    absolute = urljoin(base, raw)
    try:
        parts = urlsplit(absolute)
        host = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise MicrosoftIRUrlError(f"URL 结构非法：{raw[:40]!r}") from exc
    if parts.scheme != "https":
        raise MicrosoftIRUrlError(f"必须为 HTTPS，当前 scheme：{parts.scheme or '未知'}")
    if not host or host not in OFFICIAL_HOSTS:
        raise MicrosoftIRUrlError(f"非微软官方 host：{host}")
    if parts.username is not None or parts.password is not None:
        raise MicrosoftIRUrlError(f"URL 含 userinfo：{raw[:40]!r}")
    if port is not None and port != 443:
        raise MicrosoftIRUrlError(f"非默认端口：{port}")
    # 裸域归一化为 www.microsoft.com（两官方 host 映射同一 external_id）；
    # 显式 :443 也归一化掉。IR 内容页不依赖 query；query/fragment 均去掉，
    # 避免 utm 等追踪参数让同一份财报生成多个 external_id。
    netloc = "www.microsoft.com"
    normalized = urlunsplit(
        (parts.scheme, netloc, parts.path or "/", "", "")
    )
    if len(normalized) > EXTERNAL_ID_MAX_LENGTH:
        raise MicrosoftIRUrlError(f"URL 超过 {EXTERNAL_ID_MAX_LENGTH} 字符。")
    return normalized


class _HrefCollector(HTMLParser):
    """按文档顺序收集所有 <a href>。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hrefs = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "a":
            return
        for key, value in attrs:
            if key.lower() == "href" and value:
                self.hrefs.append(value)


def discover_earnings_links(html, base=HOME_URL):
    """从 IR 首页 HTML 发现财报页链接，返回 [{"url","fy","quarter"}, ...]。

    - 相对/绝对 URL 均按 base 规范化；
    - 无法可靠分类（非官方 host、非财报模式）的链接忽略；
    - 规范化后相同的 URL 去重，保持首次出现顺序。
    """
    collector = _HrefCollector()
    collector.feed(str(html))
    found = []
    seen = set()
    for href in collector.hrefs:
        try:
            normalized = normalize_url(href, base=base)
        except MicrosoftIRUrlError:
            continue
        parts = urlsplit(normalized)
        match = EARNINGS_PATH_PATTERN.match(parts.path)
        if not match:
            continue
        fy = int(match.group(1))
        quarter = int(match.group(2))
        canonical_url = (
            "https://www.microsoft.com/en-us/investor/earnings/"
            f"fy-{fy}-q{quarter}/press-release-webcast"
        )
        if canonical_url in seen:
            continue
        seen.add(canonical_url)
        found.append(
            {
                "url": canonical_url,
                "fy": fy,
                "quarter": quarter,
            }
        )
    return found


class _TextExtractor(HTMLParser):
    """提取 <title>、time[datetime] 日期与正文纯文本。

    script/style/nav/head 等整体跳过；优先只提取微软财报页固定的
    #pressreleasecontent 区域，页面结构变化或测试夹具没有该区域时回退正文；
    块级标签、列表项和表格行保留为可读的纯文本换行。
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title_parts = []
        self.dates = []
        self.chunks = []
        self.release_chunks = []
        self._skip_depth = 0
        self._head_depth = 0
        self._release_div_depth = 0
        self._table_depth = 0
        self._capturing_title = False
        self._title_done = False

    def _append_text(self, value):
        self.chunks.append(value)
        if self._release_div_depth > 0:
            self.release_chunks.append(value)

    def handle_starttag(self, tag, attrs):
        name = tag.lower()
        attr_map = {
            key.lower(): value for key, value in attrs if value is not None
        }
        if self._release_div_depth > 0 and name == "div":
            self._release_div_depth += 1
        elif name == "div" and attr_map.get("id", "").lower() == "pressreleasecontent":
            self._release_div_depth = 1
        if name == "head":
            self._head_depth += 1
        if name in SKIP_TAGS:
            self._skip_depth += 1
        if name == "title" and self._head_depth > 0 and not self._title_done:
            self._capturing_title = True
        if name == "time":
            for key, value in attrs:
                if key.lower() == "datetime" and value:
                    parsed = _parse_date(value)
                    if parsed is not None:
                        self.dates.append(parsed)
        if name == "meta":
            # <meta property="article:published_time"> / <meta name="date">
            value = None
            if (attr_map.get("property") or "").lower() == "article:published_time":
                value = attr_map.get("content")
            elif (attr_map.get("name") or "").lower() == "date":
                value = attr_map.get("content")
            if value:
                parsed = _parse_date(value)
                if parsed is not None:
                    self.dates.append(parsed)
        if name == "table":
            self._append_text("\n")
            self._table_depth += 1
        elif name == "tr":
            self._append_text("\n")
        elif name in BLOCK_TAGS and self._table_depth == 0:
            self._append_text("\n")
        if name == "li" and self._table_depth == 0:
            self._append_text("• ")

    def handle_endtag(self, tag):
        name = tag.lower()
        if name == "title" and self._capturing_title:
            self._capturing_title = False
            self._title_done = True
        if name in SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        if name == "head" and self._head_depth > 0:
            self._head_depth -= 1
        if name in {"td", "th"}:
            self._append_text(" | ")
        if name == "tr":
            self._append_text("\n")
        elif name == "table":
            self._append_text("\n")
            if self._table_depth > 0:
                self._table_depth -= 1
        elif name in BLOCK_TAGS and self._table_depth == 0:
            self._append_text("\n")
        if name == "div" and self._release_div_depth > 0:
            self._release_div_depth -= 1

    def handle_data(self, data):
        # <title> 位于被跳过的 <head> 内，但标题必须采集：优先 title。
        if self._capturing_title:
            self.title_parts.append(data)
        elif self._skip_depth > 0:
            return
        else:
            # HTML 源码常为排版而在句子中换行；这些不是正文段落。
            # 先压成普通空格，仅保留由块级标签显式加入的结构换行。
            self._append_text(re.sub(r"\s+", " ", data))

    def finish(self):
        title = _collapse_whitespace("".join(self.title_parts))
        selected = self.release_chunks or self.chunks
        text = _normalize_plain_text("".join(selected))
        published = min(self.dates) if self.dates else None
        return title, published, text


def _parse_date(value):
    """可靠解析 ISO 日期（YYYY-MM-DD 前缀）；解析不了返回 None。"""
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return None


def _collapse_whitespace(text):
    return re.sub(r"\s+", " ", str(text)).strip()


def _normalize_plain_text(text):
    """压缩行内空白，同时保留提取器产生的内容结构。"""
    lines = []
    for raw_line in str(text).replace("\xa0", " ").splitlines():
        line = re.sub(r"[^\S\r\n]+", " ", raw_line).strip()
        line = line.rstrip(" |").strip()
        if line:
            lines.append(line)
    return "\n\n".join(lines)


def _parse_release_dateline(text):
    """解析 Microsoft 财报正文固定发布行；无法可靠识别时返回 None。"""
    match = RELEASE_DATELINE_PATTERN.search(text[:5000])
    if not match:
        return None
    try:
        return datetime.strptime(
            f"{match.group(1)} {match.group(2)}, {match.group(3)}", "%B %d, %Y"
        ).date()
    except ValueError:
        return None


def extract_page_content(html, max_content_chars=DEFAULT_MAX_CONTENT_CHARS):
    """从财报页 HTML 提取 {"title","published_at","content_text"}。

    - 无标题 → MicrosoftIRParseError（页面结构变化）；
    - 正文为空 → MicrosoftIREmptyBodyError；
    - 正文超 max_content_chars → MicrosoftIRParseError。
    """
    if not isinstance(max_content_chars, int) or max_content_chars <= 0:
        raise MicrosoftIRConfigError(f"max_content_chars 非法：{max_content_chars!r}")
    extractor = _TextExtractor()
    try:
        extractor.feed(str(html))
        extractor.close()
    except Exception as exc:  # HTMLParser 异常（如未闭合实体）视为结构异常
        raise MicrosoftIRParseError(f"HTML 解析失败：{exc}") from exc
    title, published, text = extractor.finish()
    if not title:
        raise MicrosoftIRParseError("页面缺少可识别标题，结构可能已变化。")
    if not text:
        raise MicrosoftIREmptyBodyError("正文为空，不算成功抓取。")
    if len(text) > max_content_chars:
        raise MicrosoftIRParseError(
            f"正文 {len(text)} 字符超过上限 {max_content_chars}。"
        )
    if published is None:
        published = _parse_release_dateline(text)
    return {
        "title": title[:TITLE_MAX_LENGTH],
        "published_at": published,
        "content_text": text,
    }


class _OfficialRedirectHandler(urllib.request.HTTPRedirectHandler):
    """默认 opener 的跳转校验：跳到非官方 host 即失败。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            target = urlsplit(newurl)
            target_host = target.hostname
            target_port = target.port
        except ValueError as exc:
            raise MicrosoftIRUrlError("跳转 URL 结构非法。") from exc
        if (
            target.scheme != "https"
            or not target_host
            or target_host not in OFFICIAL_HOSTS
            or target.username is not None
            or (target_port is not None and target_port != 443)
        ):
            raise MicrosoftIRUrlError(
                f"跳转到非微软官方 host：{target_host or newurl!r}"
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class MicrosoftIRClient:
    """微软 IR 官方站点客户端；opener 可注入，离线测试用 mock。

    鸭子接口：fetch_home() -> str；fetch_earnings_page(url) -> dict。
    """

    def __init__(
        self,
        *,
        opener=None,
        user_agent=DEFAULT_USER_AGENT,
        timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
        max_response_bytes=DEFAULT_MAX_RESPONSE_BYTES,
        max_content_chars=DEFAULT_MAX_CONTENT_CHARS,
    ):
        if not user_agent or not str(user_agent).strip():
            raise MicrosoftIRConfigError("user_agent 不能为空。")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise MicrosoftIRConfigError(f"timeout_seconds 非法：{timeout_seconds!r}")
        if (
            isinstance(max_response_bytes, bool)
            or not isinstance(max_response_bytes, int)
            or max_response_bytes <= 0
        ):
            raise MicrosoftIRConfigError(
                f"max_response_bytes 非法：{max_response_bytes!r}"
            )
        if (
            isinstance(max_content_chars, bool)
            or not isinstance(max_content_chars, int)
            or max_content_chars <= 0
        ):
            raise MicrosoftIRConfigError(
                f"max_content_chars 非法：{max_content_chars!r}"
            )
        self.user_agent = str(user_agent).strip()
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max_response_bytes
        self.max_content_chars = max_content_chars
        self._opener = opener or urllib.request.build_opener(_OfficialRedirectHandler())

    def fetch_home(self):
        """抓取 IR 首页 HTML 文本。"""
        return self._fetch_text(HOME_URL)

    def fetch_earnings_page(self, url):
        """抓取并解析财报页，返回 {"title","published_at","content_text"}。"""
        normalized = normalize_url(url)
        html = self._fetch_text(normalized)
        return extract_page_content(html, max_content_chars=self.max_content_chars)

    def _fetch_text(self, url):
        request = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
        try:
            response = self._opener.open(request, timeout=self.timeout_seconds)
        except urllib.error.HTTPError as exc:
            raise MicrosoftIRHTTPError(
                f"官方站点返回 HTTP {exc.code}", status=exc.code
            ) from exc
        except TimeoutError as exc:
            raise MicrosoftIRTimeoutError(f"请求超时：{url}") from exc
        except urllib.error.URLError as exc:
            raise MicrosoftIRNetworkError(f"网络错误：{exc.reason}") from exc
        except OSError as exc:
            raise MicrosoftIRNetworkError(f"网络错误：{exc}") from exc
        try:
            try:
                body = response.read(self.max_response_bytes + 1)
            except TimeoutError as exc:
                raise MicrosoftIRTimeoutError(f"读取响应超时：{url}") from exc
            except urllib.error.URLError as exc:
                raise MicrosoftIRNetworkError(f"读取响应失败：{exc.reason}") from exc
            except OSError as exc:
                raise MicrosoftIRNetworkError(f"读取响应失败：{exc}") from exc
        finally:
            response.close()
        if len(body) > self.max_response_bytes:
            raise MicrosoftIRResponseTooLarge(
                f"响应超过 {self.max_response_bytes} 字节。"
            )
        try:
            return body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise MicrosoftIRParseError(f"响应非 UTF-8 文本：{exc}") from exc
