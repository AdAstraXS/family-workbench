"""Filter search leads and archive bounded public originals, never snippet-only facts."""
import hashlib
import re
from datetime import date, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from django.utils import timezone

from knowledge.web_fetch import canonical_url, public_request, WebCaptureError
from .ir_extraction import extract_material
from .preparation_research import PATTERNS, digest, excerpts, public_identity
from .providers.ir_registry import company_for_security

MAX_FETCHES = 3
NOISE = re.compile(r'coupon|promo code|weekly (?:ad|deals)|savings event|earnings calendar|'
                   r'促销|优惠券|折扣码|业绩日历|财经日历|特价商品', re.I)


def canonical_article(url):
    parts = urlsplit(canonical_url(url))
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith('utm_') and k.lower() not in {'spm', 'fbclid', 'gclid', 'referrer'}]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ''))


def normalized(text):
    return re.sub(r'[^\w\u3400-\u9fff]', '', text.lower())


def same_article(left, right):
    a, b = normalized(left), normalized(right)
    if not a or not b:
        return False
    if a == b:
        return True
    # Reprints commonly change their heading while retaining the same opening paragraphs.
    if min(len(a), len(b)) < 100:
        return False
    first, second = ({s[i:i+8] for i in range(0, min(len(s), 1200) - 7, 4)} for s in (a, b))
    return len(first & second) / max(1, min(len(first), len(second))) >= .82


def is_official_url(security, url):
    p = urlsplit(url)
    if p.hostname in {'www.sec.gov', 'sec.gov'} and p.path.startswith('/Archives/edgar/data/'):
        return True
    company = company_for_security(security)
    if not company:
        return False
    entry = urlsplit(company.entry)
    return p.hostname == entry.hostname or any(p.hostname == host and p.path.startswith(prefix)
                                              for host, prefix in company.assets)


def published_date(value):
    match = re.search(r'\b(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})', str(value))
    try:
        return date(*map(int, match.groups())) if match else None
    except ValueError:
        return None


def stale_reason(value, period):
    stamp = published_date(value)
    if not stamp:
        return ''
    today = timezone.localdate()
    if stamp > today + timedelta(days=1):
        return '发布日期在未来，未采用'
    if period == 'latest' and stamp < today - timedelta(days=370):
        return '不适合作为近期资料，未采用'
    if period != 'latest' and stamp.year < int(period):
        return '发布日期早于待核对年度，未采用'
    return ''


def mentions_issuer(text, identity):
    return any(alias.lower() in text.lower() for alias in identity['aliases']) or bool(
        re.search(r'(?<!\w)' + re.escape(identity['symbol']) + r'(?!\w)', text))


def fetch_original(url):
    # Redirects are rejected so an official URL cannot silently become a third-party source.
    raw, content_type = public_request(url, limit=2 * 1024 * 1024, timeout=18, redirects=0)
    mime = content_type.split(';', 1)[0].lower().strip()
    if mime not in {'text/html', 'application/xhtml+xml', 'application/pdf'}:
        raise WebCaptureError('本轮仅支持 HTML 或 PDF 原文。')
    extracted = extract_material(SimpleNamespace(url=url, raw=raw, content_type=mime))
    text = extracted['text']
    if len(text) > 120_000:
        raise WebCaptureError('正文超过本轮归档上限，保留为待人工阅读线索。')
    return {'text': text, 'sha256': digest(text), 'raw_sha256': hashlib.sha256(raw).hexdigest(),
            'media_type': extracted['media_type'],
            'published_at': str(extracted.get('published_at') or ''),
            'fetched_at': timezone.now().isoformat()}


def collect_originals(security, rows, plans, *, fetcher=None, checkpoint=None):
    """At most three fetches total; every discarded lead retains a reason."""
    fetcher = fetcher or fetch_original
    identity = public_identity(security)
    by_query = {p['query']: p for p in plans}
    candidates, receipts, originals, accepted = [], [], [], []
    seen_urls, seen_titles, seen_texts = set(), set(), []
    for row in rows[:10]:
        receipt = {'title': row['title'], 'url': row['url'], 'date': row['date'],
                   'query': row.get('query', ''), 'status': '未采用', 'reason': ''}
        receipts.append(receipt)
        plan = by_query.get(row.get('query'))
        try:
            url = canonical_article(row['url'])
        except (ValueError, TypeError):
            receipt['reason'] = '链接不符合公开网页规则'
            continue
        if not plan:
            receipt['reason'] = '不属于本次定向查询'
            continue
        title = normalized(row['title'])
        if url in seen_urls or (title and title in seen_titles) or any(same_article(row['text'], text) for text in seen_texts):
            receipt['reason'] = '重复链接或疑似转载，未重复计为证据'
            continue
        seen_urls.add(url)
        seen_titles.add(title)
        seen_texts.append(row['text'])
        preview = row['title'] + '\n' + row['text']
        reason = ('促销或日历页面，与研究问题不符' if NOISE.search(row['title']) else
                  stale_reason(row['date'], plan['period']))
        if not reason and not PATTERNS[plan['topic']].search(preview):
            reason = '搜索摘要未匹配待核查主题'
        if not reason and not mentions_issuer(preview, identity):
            reason = '搜索摘要未能确认对应公司'
        if reason:
            receipt['reason'] = reason
            continue
        candidates.append((not is_official_url(security, url), row, plan, receipt, url))
    # Official originals get the first available slots without increasing the request count.
    candidates.sort(key=lambda item: item[0])
    for index, (_, row, plan, receipt, url) in enumerate(candidates):
        if index >= MAX_FETCHES:
            receipt['reason'] = '已达到本轮三篇原文请求上限'
            continue
        receipt.update(status='正在获取', reason='')
        if checkpoint:
            checkpoint(receipts, originals)
        try:
            original = fetcher(url)
        except Exception:
            receipt.update(status='原文未取得', reason='访问受限、格式不支持或超出限额；摘要不进入事实证据')
            continue
        text = original['text']
        reason = stale_reason(original.get('published_at') or row['date'], plan['period'])
        if not reason and not mentions_issuer(text, identity):
            reason = '原文未能确认对应公司'
        if not reason and not PATTERNS[plan['topic']].search(text):
            reason = '原文没有匹配待核查主题'
        if not reason and any(same_article(text, saved['text']) for saved in originals):
            reason = '正文与已取得资料重复，未重复计为独立证据'
        if reason:
            receipt.update(status='未采用', reason=reason)
            continue
        number = len(originals) + 1
        original.update(title=row['title'], url=url, date=original.get('published_at') or row['date'],
                        number=number, topic=plan['topic'])
        originals.append(original)
        receipt.update(status='原文已取得', reason='已保存正文；是否解决问题仍需最终分析', original_number=number)
        # Financial tables keep their headers and periods when available.
        from .financial_excerpts import statement_excerpts
        spans = statement_excerpts(text)[:2] if plan['topic'] in {'earnings', 'cash_flow'} else []
        spans = spans or excerpts(text, plan['topic'], limit=2, length=1500)
        for quote, offset in spans:
            if len(quote) > 5000:
                continue
            if not is_official_url(security, url) and not mentions_issuer(quote, identity):
                continue
            accepted.append({'title': row['title'], 'kind': 'web_original', 'date': original['date'],
                'url': url, 'original_number': number, 'fetched_at': original['fetched_at'], 'topic': plan['topic'],
                'source_note': '官方原文摘录' if is_official_url(security, url) else '第三方原文摘录，需核对其证据来源',
                'text': quote, 'offset': offset, 'excerpt_sha256': digest(quote)})
        if checkpoint:
            checkpoint(receipts, originals)
    return accepted, receipts, originals
