"""SEC EDGAR 官方接口客户端（M2A-2）。

只用标准库 urllib；opener/clock/sleeper 可注入，离线测试全部 mock。
规则：
- 只接受 HTTPS 与固定 SEC 官方主机（www.sec.gov / data.sec.gov）；
- 每次请求都发送配置的 User-Agent（SEC 要求明确 UA）；
- 429/503 做有限次数线性退避重试，其他 HTTP 错误不盲目重试；
- 响应体超过上限抛错，错误信息不得包含完整响应正文。
"""
import json
import time
import urllib.error
import urllib.request
from datetime import datetime
from urllib.parse import unquote, urlsplit

OFFICIAL_HOSTS = frozenset({"www.sec.gov", "data.sec.gov"})
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL_TEMPLATE = "https://data.sec.gov/submissions/CIK{cik}.json"
ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data"

ALLOWED_FORMS = frozenset({"10-K", "10-Q", "8-K"})
FORM_TO_DOCUMENT_TYPE = {"10-K": "10-k", "10-Q": "10-q", "8-K": "8-k"}
REQUIRED_FILING_COLUMNS = (
    "accessionNumber",
    "filingDate",
    "form",
    "primaryDocument",
    "reportDate",
)
RETRY_STATUSES = frozenset({429, 503})
_TITLE_MAX_LENGTH = 500
_CIK_DIGITS = 10


class SecClientError(Exception):
    """SEC 客户端错误基类；message 可直接展示，不含完整响应正文。"""


class SecConfigError(SecClientError):
    """配置缺失或非法（例如未配置 User-Agent）。"""


class SecHTTPError(SecClientError):
    """SEC 返回非重试类 HTTP 状态，或重试耗尽。"""

    def __init__(self, message, *, status=None):
        super().__init__(message)
        self.status = status


class SecNetworkError(SecClientError):
    """连接级网络错误（DNS/拒绝/中断等），不盲目重试。"""


class SecTimeoutError(SecClientError):
    """请求超时。"""


class SecResponseTooLarge(SecClientError):
    """响应体超过大小上限。"""


class SecTickerNotFoundError(SecClientError):
    """ticker 映射中找不到对应公司，或映射结构异常。"""


class SecFilingsParseError(SecClientError):
    """submissions 列式 JSON 解析失败（缺列/长度不一致/日期非法）。"""


class SecDocumentUrlError(SecClientError):
    """primaryDocument/accession 不合法，无法构造 Archives URL。"""


def _validate_sec_request_url(url, *, archives_only=False):
    parts = urlsplit(url)
    try:
        port = parts.port
    except ValueError as exc:
        raise SecClientError("SEC 请求地址的端口无效。") from exc
    if parts.scheme != "https":
        raise SecClientError("SEC 请求必须使用 HTTPS。")
    if (parts.hostname not in OFFICIAL_HOSTS or parts.username
            or parts.password or port not in (None, 443)):
        raise SecClientError("SEC 请求只允许无凭据的官方主机。")
    if archives_only and (parts.hostname != "www.sec.gov"
                          or not parts.path.startswith("/Archives/edgar/data/")
                          or parts.query or parts.fragment):
        raise SecDocumentUrlError("SEC 正文必须来自官方 Archives 路径。")


class _OfficialRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        archives_only = urlsplit(req.full_url).path.startswith("/Archives/edgar/data/")
        _validate_sec_request_url(newurl, archives_only=archives_only)
        if archives_only and newurl != req.full_url:
            raise SecDocumentUrlError("SEC 正文重定向到其他文件，已停止抓取。")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _validate_positive_number(value, label, *, maximum=None):
    """数值配置校验：必须是 int/float（bool 不算），大于 0，可选上限。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SecConfigError(f"{label} 应为数值：{value!r}")
    if value <= 0:
        raise SecConfigError(f"{label} 应大于 0，当前为 {value!r}。")
    if maximum is not None and value > maximum:
        raise SecConfigError(f"{label} 不应超过 {maximum}，当前为 {value!r}。")


def _validate_non_negative_number(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SecConfigError(f"{label} 应为数值：{value!r}")
    if value < 0:
        raise SecConfigError(f"{label} 不应为负，当前为 {value!r}。")


def _pad_cik(cik):
    """把 CIK 规整为 10 位零填充字符串；非法时抛 SecClientError。"""
    text = str(cik).strip()
    if not text.isdigit():
        raise SecClientError(f"CIK 应为纯数字：{text[:20]}")
    if len(text) > _CIK_DIGITS:
        raise SecClientError(f"CIK 超过 {_CIK_DIGITS} 位：{text[:20]}")
    return text.zfill(_CIK_DIGITS)


def _parse_sec_date(value, label, *, allow_empty=False):
    text = str(value or "").strip()
    if not text:
        if allow_empty:
            return None
        raise SecFilingsParseError(f"{label} 为空。")
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError as exc:
        raise SecFilingsParseError(f"{label} 日期非法：{text[:20]}") from exc


def _build_title(company_name, form, filing_date):
    if company_name:
        title = f"{company_name} {form} ({filing_date.isoformat()})"
    else:
        title = f"{form} ({filing_date.isoformat()})"
    return title[:_TITLE_MAX_LENGTH]


def parse_recent_filings(data, *, company_name=""):
    """解析 data.sec.gov submissions 的 filings.recent 列式结构。

    只保留 10-K / 10-Q / 8-K（含 /A 修正案，按基础表类型归类）。
    返回按接口原始顺序（最新在前）的记录列表。
    """
    if not isinstance(data, dict):
        raise SecFilingsParseError("submissions 响应结构异常。")
    filings = data.get("filings")
    recent = filings.get("recent") if isinstance(filings, dict) else None
    if not isinstance(recent, dict):
        raise SecFilingsParseError("缺少 filings.recent 列。")
    for column in REQUIRED_FILING_COLUMNS:
        if column not in recent:
            raise SecFilingsParseError(f"缺少 {column} 列。")
        if not isinstance(recent[column], list):
            raise SecFilingsParseError(f"{column} 列应为 JSON 列表，当前为 {type(recent[column]).__name__}。")
    length = len(recent["accessionNumber"])
    for column in REQUIRED_FILING_COLUMNS:
        if len(recent[column]) != length:
            raise SecFilingsParseError(f"{column} 列长度与 accessionNumber 不一致。")

    records = []
    for index in range(length):
        form = str(recent["form"][index]).strip()
        base_form = form[:-2] if form.endswith("/A") else form
        if base_form not in ALLOWED_FORMS:
            continue
        accession = str(recent["accessionNumber"][index]).strip()
        if not accession:
            raise SecFilingsParseError("存在空 accession。")
        filing_date = _parse_sec_date(recent["filingDate"][index], "filingDate")
        report_date = _parse_sec_date(
            recent["reportDate"][index], "reportDate", allow_empty=True
        )
        records.append(
            {
                "accession": accession,
                "form": form,
                "document_type": FORM_TO_DOCUMENT_TYPE[base_form],
                "filing_date": filing_date,
                "report_date": report_date,
                "primary_document": str(recent["primaryDocument"][index]).strip(),
                "title": _build_title(company_name, form, filing_date),
            }
        )
    return records


def filing_url(cik, accession, primary_document):
    """按 CIK、去连字符 accession、primaryDocument 构造 Archives HTTPS URL。

    拒绝外部 URL、绝对路径、路径穿越、空文件名和非法 accession。
    """
    padded = _pad_cik(cik)
    clean_accession = str(accession).strip().replace("-", "")
    if not clean_accession.isdigit() or not (10 <= len(clean_accession) <= 20):
        raise SecDocumentUrlError(
            f"accession 应为 10-20 位数字（去连字符后）：{str(accession)[:20]}"
        )
    document = str(primary_document or "").strip()
    if not document:
        raise SecDocumentUrlError("primaryDocument 为空。")
    if any(ord(char) < 32 or ord(char) == 127 for char in document):
        raise SecDocumentUrlError(f"primaryDocument 含控制字符：{document[:40]!r}")
    if "://" in document or document.startswith(("/", "\\")) or "\\" in document:
        raise SecDocumentUrlError(f"primaryDocument 应为相对路径：{document[:40]}")
    if "?" in document or "#" in document:
        raise SecDocumentUrlError(f"primaryDocument 不应含 query/fragment：{document[:40]}")
    # 百分号解码后再查路径段，拦截 ..%2f、%2e%2e 等编码穿越
    for part in unquote(document).split("/"):
        if part in ("", ".", ".."):
            raise SecDocumentUrlError(f"primaryDocument 含非法路径段：{document[:40]}")
    return f"{ARCHIVES_BASE}/{padded.lstrip('0') or '0'}/{clean_accession}/{document}"


class SecClient:
    """SEC EDGAR 客户端；opener/clock/sleeper 注入用于离线测试。"""

    def __init__(
        self,
        *,
        user_agent,
        timeout=10.0,
        max_response_bytes=10 * 1024 * 1024,
        rate_limit_per_second=5.0,
        max_retries=3,
        backoff_seconds=0.5,
        opener=None,
        clock=None,
        sleeper=None,
    ):
        if not user_agent or not str(user_agent).strip():
            raise SecConfigError(
                "RESEARCH_SEC_USER_AGENT 未配置；SEC 要求明确的 User-Agent。"
            )
        _validate_positive_number(timeout, "timeout")
        _validate_positive_number(max_response_bytes, "max_response_bytes")
        _validate_positive_number(
            rate_limit_per_second, "rate_limit_per_second", maximum=5.0
        )
        if isinstance(max_retries, bool) or not isinstance(max_retries, int):
            raise SecConfigError(f"max_retries 应为整数：{max_retries!r}")
        if max_retries < 0:
            raise SecConfigError(f"max_retries 不应为负，当前为 {max_retries!r}。")
        _validate_non_negative_number(backoff_seconds, "backoff_seconds")
        self.user_agent = str(user_agent).strip()
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes
        self.rate_limit_per_second = rate_limit_per_second
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds
        self._opener = opener or urllib.request.build_opener(_OfficialRedirectHandler()).open
        self._clock = clock or time.monotonic
        self._sleeper = sleeper or time.sleep
        self._last_request_at = None

    # -- 底层请求 -------------------------------------------------------

    def _throttle(self):
        interval = 1.0 / self.rate_limit_per_second
        now = self._clock()
        if self._last_request_at is not None:
            wait_until = self._last_request_at + interval
            if now < wait_until:
                self._sleeper(wait_until - now)
                now = self._clock()
        self._last_request_at = now

    def _get(self, url, *, max_bytes=None, archives_only=False):
        _validate_sec_request_url(url, archives_only=archives_only)
        limit = self.max_response_bytes if max_bytes is None else max_bytes
        _validate_positive_number(limit, "max_bytes")
        request = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
        attempt = 0
        while True:
            attempt += 1
            self._throttle()
            try:
                response = self._opener(request, timeout=self.timeout)
            except urllib.error.HTTPError as exc:
                if exc.code in RETRY_STATUSES and attempt <= self.max_retries:
                    self._sleeper(self.backoff_seconds * attempt)
                    continue
                message = (
                    "SEC 返回 HTTP 403；可能是访问限流或出口受限，请暂缓重试并检查 SEC 访问状态。"
                    if exc.code == 403 else f"SEC 返回 HTTP {exc.code}"
                )
                raise SecHTTPError(
                    message, status=exc.code
                ) from exc
            except TimeoutError as exc:
                raise SecTimeoutError(f"SEC 请求超时（{self.timeout}s）。") from exc
            except urllib.error.URLError as exc:
                raise SecNetworkError(f"SEC 网络错误：{exc.reason}") from exc
            except OSError as exc:
                # ConnectionResetError 等底层 OSError 统一归为网络错误，不泄漏原文
                raise SecNetworkError(f"SEC 连接异常：{type(exc).__name__}") from exc
            with response:
                if hasattr(response, "geturl"):
                    final_url = response.geturl()
                    _validate_sec_request_url(final_url, archives_only=archives_only)
                    if archives_only and final_url != url:
                        raise SecDocumentUrlError("SEC 正文最终链接与所选文件不同。")
                body = response.read(limit + 1)
            if len(body) > limit:
                raise SecResponseTooLarge(
                    f"SEC 响应超过 {limit} 字节上限。"
                )
            return body

    def get_document_html(self, url, *, max_bytes):
        """仅获取已校验的 SEC Archives 主 HTML；不跟随站外跳转。"""
        return self._get(url, max_bytes=max_bytes, archives_only=True)

    def get_json(self, url):
        body = self._get(url)
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise SecFilingsParseError("SEC 响应不是有效 JSON。") from exc

    # -- 官方接口 -------------------------------------------------------

    def resolve_cik(self, ticker):
        """通过 company_tickers.json 解析 ticker → 10 位零填充 CIK。"""
        data = self.get_json(TICKERS_URL)
        if not isinstance(data, dict):
            raise SecTickerNotFoundError("SEC ticker 映射结构异常。")
        wanted = str(ticker).strip().upper()
        for value in data.values():
            if isinstance(value, dict) and str(value.get("ticker", "")).upper() == wanted:
                return _pad_cik(value.get("cik_str") or value.get("cik"))
        raise SecTickerNotFoundError(f"SEC ticker 映射中没有 {wanted}。")

    def get_filings(self, cik):
        """拉取 submissions 并解析为最近 filings 记录列表（最新在前）。"""
        url = SUBMISSIONS_URL_TEMPLATE.format(cik=_pad_cik(cik))
        data = self.get_json(url)
        company_name = str(data.get("name") or "").strip() if isinstance(data, dict) else ""
        return parse_recent_filings(data, company_name=company_name)
