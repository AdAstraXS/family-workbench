"""Small, reviewed source catalogue. Public publisher text precedes paid ASR."""
import hashlib
import json
import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from django.db.models import Q
from django.utils import timezone
from datetime import timedelta

from .adapters import _safe_xml_root, _first_text, _children, _entry_link, _parse_datetime, parse_youtube_atom
from .http_client import fetch_public_url
from .program_models import ProgramEntry, ProgramSubscription

CATALOGUE = {
    'rhino': {'name': '视野环球财经', 'layer': '每日持仓观察', 'kind': 'youtube',
              'url': 'https://www.youtube.com/@RhinoFinance',
              'feed': 'https://www.youtube.com/feeds/videos.xml?channel_id=UCFQsi7WaF5X41tcuOryDk8w',
              'channel_id': 'UCFQsi7WaF5X41tcuOryDk8w', 'description': '每日完整节目，突出持仓相关观点，保留市场判断。'},
    'dwarkesh': {'name': 'Dwarkesh Podcast', 'layer': '完整访谈与演讲', 'kind': 'rss',
                 'url': 'https://www.dwarkesh.com/', 'feed': 'https://www.dwarkesh.com/feed',
                 'description': '技术人物深度访谈，优先官方文字稿。'},
    'good_company': {'name': 'In Good Company', 'layer': '完整访谈与演讲', 'kind': 'rss',
                     'url': 'https://www.nbim.no/en/news-and-insights/podcast/',
                     'feed': 'https://feeds.acast.com/public/shows/in-good-company-with-nicolai-tangen',
                     'description': 'Nicolai Tangen 的公司领导者访谈，排除 Highlights 和每周回顾剪辑。'},
    'oaktree': {'name': 'Howard Marks · Oaktree Memos', 'layer': '投资方法与风险判断', 'kind': 'memo',
               'url': 'https://www.oaktreecapital.com/insights', 'feed': 'https://www.oaktreecapital.com/insights',
               'description': '官方备忘录原文，关注周期、估值、风险与投资方法。'},
}


class ProgramError(ValueError):
    """A safe, user-visible message; never include remote bodies, credentials or signed URLs."""


class ProgramConfigurationRequired(ProgramError):
    pass


def plain_segments(html):
    soup = BeautifulSoup(html, 'html.parser')
    for node in soup.select('script,style,nav,footer,form,button,iframe'):
        node.decompose()
    blocks = [node.get_text(' ', strip=True) for node in soup.select('p,h2,h3,h4,li') if not node.find(['p', 'li'])]
    if not blocks:
        blocks = [line.strip() for line in soup.get_text('\n').splitlines() if line.strip()]
    return [{'text': text, 'start_ms': None, 'end_ms': None} for text in blocks if text]


def duration_seconds(value):
    try:
        parts = [float(p) for p in str(value).split(':')]
        return int(sum(n * 60 ** i for i, n in enumerate(reversed(parts))))
    except (ValueError, OverflowError):
        return 0


def parse_catalogue_feed(code, body):
    spec = CATALOGUE[code]
    if spec['kind'] == 'youtube':
        return [dict(external_id=i.external_id, title=i.title, url=i.canonical_url, published_at=i.published_at)
                for i in parse_youtube_atom(body, expected_channel_id=spec['channel_id'])]
    if spec['kind'] == 'memo':
        soup = BeautifulSoup(body, 'html.parser')
        result, seen = [], set()
        for node in soup.select('[data-items]'):
            try:
                rows = json.loads(node['data-items'])
            except (ValueError, TypeError):
                continue
            for row in rows:
                url = urljoin(spec['url'], row.get('MoreLink', '')).split('?')[0]
                if urlsplit(url).hostname != 'www.oaktreecapital.com' or '/insights/memo/' not in url or url in seen:
                    continue
                seen.add(url)
                result.append(dict(external_id=hashlib.sha256(url.encode()).hexdigest(), title=row.get('Title', '')[:500], url=url,
                                   published_at=_parse_datetime(row.get('IsoDate', ''))))
        for a in soup.select('a[href]'):
            url = urljoin(spec['url'], a['href']).split('?')[0]
            if urlsplit(url).hostname != 'www.oaktreecapital.com' or '/insights/memo/' not in url or url in seen:
                continue
            title = a.get_text(' ', strip=True)
            if not title:
                continue
            seen.add(url)
            result.append(dict(external_id=hashlib.sha256(url.encode()).hexdigest(), title=title[:500], url=url))
        if not result:
            raise ProgramError('未找到官方备忘录列表，页面结构可能已变化。')
        return result[:30]
    root = _safe_xml_root(body)
    channels = _children(root, 'channel')
    entries = _children(channels[0], 'item') if channels else _children(root, 'entry')
    result = []
    for item in entries[:80]:
        title = _first_text(item, 'title')
        if code == 'good_company' and re.search(r'(?i)highlights|friday wrap|trailer', title):
            continue
        url = _entry_link(item, spec['url'])
        if not url:
            continue
        content = _first_text(item, 'encoded', 'content')
        enclosures = _children(item, 'enclosure')
        audio = enclosures[0].get('url', '') if enclosures else ''
        # A description is not a transcript, even if it is long.
        segments = plain_segments(content) if code == 'dwarkesh' and re.search(r'(?i)transcript', content) and len(content) > 6000 else []
        result.append(dict(external_id=hashlib.sha256((_first_text(item, 'guid', 'id') or url).encode()).hexdigest(),
                           title=title[:500], url=url, audio_url=audio, duration_seconds=duration_seconds(_first_text(item, 'duration')),
                           published_at=_parse_datetime(_first_text(item, 'pubdate', 'published')), segments=segments))
    return result


def fetch_publisher_text(entry):
    code = entry.subscription.code
    parsed = urlsplit(entry.url)
    permitted = {'dwarkesh': {'www.dwarkesh.com', 'dwarkesh.com'}, 'oaktree': {'www.oaktreecapital.com'}}
    if parsed.hostname not in permitted.get(code, set()):
        return []
    response = fetch_public_url(entry.url, max_bytes=5000000)
    if urlsplit(response.url).hostname not in permitted[code]:
        raise ProgramError('正文跳转到了未批准的网站。')
    soup = BeautifulSoup(response.body, 'html.parser')
    if code == 'dwarkesh':
        article = soup.select_one('.available-content .body, .body.markup')
        if not article or not re.search(r'(?i)transcript', article.get_text()):
            return []
        # Subscriber previews must not be represented as complete transcripts.
        if soup.select_one('.paywall, .paywall-title, .paywall-content'):
            return []
    else:
        article = soup.select_one('.sf-detail-body, .memo-content, .insight-detail-content, .article-content .col-md-8')
        if not article:
            raise ProgramError('官方备忘录正文结构无法识别，请检查来源页面。')
    segments = plain_segments(str(article))
    return segments if sum(len(s['text']) for s in segments) >= 1000 else []


def collect_subscription(subscription):
    from .program_processing import save_revision
    now = timezone.now()
    if not ProgramSubscription.objects.filter(pk=subscription.pk, enabled=True).filter(
            Q(lease_until__isnull=True) | Q(lease_until__lt=now)).update(lease_until=now + timedelta(minutes=5)):
        return 0
    try:
        spec = CATALOGUE[subscription.code]
        items = parse_catalogue_feed(subscription.code, fetch_public_url(spec['feed'], max_bytes=8000000).body)
        initial = subscription.last_success_at is None
        items = items[:3] if initial else items[:30]
        count = 0
        for index, item in enumerate(items):
            segments = item.pop('segments', [])
            external_id = item.pop('external_id')
            published = item.get('published_at')
            # Expanding the fetched window on later polls must never enqueue old backfill.
            is_new_publication = bool(published and published >= subscription.created_at)
            entry, created = ProgramEntry.objects.get_or_create(subscription=subscription, external_id=external_id,
                defaults={**item, 'requested': subscription.auto_process and ((initial and index == 0) or (not initial and is_new_publication))})
            if created:
                count += 1
                if segments:
                    save_revision(entry, segments, origin='publisher', source_url=entry.url)
        ProgramSubscription.objects.filter(pk=subscription.pk).update(last_checked_at=now, last_success_at=now, last_error='')
        return count
    except Exception as exc:
        message = str(exc) if isinstance(exc, ProgramError) else '订阅抓取失败，请检查网络或来源页面后重试。'
        ProgramSubscription.objects.filter(pk=subscription.pk).update(last_checked_at=now, last_error=message)
        raise ProgramError(message) from exc
    finally:
        ProgramSubscription.objects.filter(pk=subscription.pk).update(lease_until=None)
