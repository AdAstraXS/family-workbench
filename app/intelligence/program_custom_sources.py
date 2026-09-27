"""Reviewed, family-configured public feeds and video channels."""
import hashlib
import json
import math
import re
from datetime import datetime, timezone as datetime_timezone
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup
from django.utils import timezone

from .adapters import _safe_xml_root, _first_text, _children, _entry_link, _parse_datetime
from .http_client import SafeHttpError
from .program_network import public_user_source_url, fetch_user_source_url
from .program_media import _yt_command
from .program_sources import CATALOGUE, ProgramError, plain_segments, duration_seconds


def source_spec(subscription):
    if subscription.code in CATALOGUE:
        return CATALOGUE[subscription.code]
    if subscription.kind not in {'youtube', 'bilibili', 'podcast', 'article', 'upload'}:
        raise ProgramError('信源类型无效。')
    return {'name': subscription.custom_name, 'kind': subscription.kind,
            'url': subscription.source_url, 'feed': subscription.feed_url,
            'channel_id': subscription.channel_id, 'playlist_id': subscription.playlist_id,
            'layer': '自选信源',
            'description': '由家庭管理员添加。'}


def check_public_url(value):
    value = value.strip()
    parsed = urlsplit(value)
    if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.fragment:
        raise ProgramError('请输入不含凭据或片段的公开 HTTPS 地址。')
    query_keys = {key.casefold() for key in parse_qs(parsed.query)}
    if (parsed.hostname or '').casefold().endswith(('patreon.com', 'patreonusercontent.com')) or query_keys & {
            'token', 'auth', 'key', 'api_key', 'access_token', 'rss_key'}:
        raise ProgramError('本版只接入公开信源；含会员凭据的链接请勿在这里保存。')
    try:
        public_user_source_url(value)
    except SafeHttpError as exc:
        raise ProgramError(exc.safe_message) from exc
    return value


def check_reference_url(value):
    value = value.strip()
    parsed = urlsplit(value)
    if parsed.fragment or {key.casefold() for key in parse_qs(parsed.query)} & {
            'token', 'auth', 'key', 'api_key', 'access_token', 'rss_key'}:
        raise ProgramError('原始链接不能包含访问令牌。')
    try:
        public_user_source_url(value)
    except SafeHttpError as exc:
        raise ProgramError(exc.safe_message) from exc
    return value


def video_source_url(kind, url):
    parsed = urlsplit(check_public_url(url))
    if kind == 'youtube':
        if parsed.hostname not in {'www.youtube.com', 'youtube.com'}:
            raise ProgramError('请输入 YouTube 频道或公开播放列表地址。')
        if parsed.path == '/playlist':
            playlist = parse_qs(parsed.query).get('list', [''])[0]
            if not re.fullmatch(r'[A-Za-z0-9_-]{10,100}', playlist):
                raise ProgramError('YouTube 播放列表编号无效。')
            return 'https://www.youtube.com/playlist?list=' + playlist
        if re.fullmatch(r'/@[A-Za-z0-9._-]{3,100}(?:/videos)?/?', parsed.path) or re.fullmatch(r'/channel/UC[A-Za-z0-9_-]{22}(?:/videos)?/?', parsed.path):
            return 'https://www.youtube.com' + parsed.path.rstrip('/').removesuffix('/videos') + '/videos'
        raise ProgramError('请输入 YouTube 的 @频道、/channel/ 编号或播放列表链接。')
    if kind == 'bilibili':
        if parsed.hostname != 'space.bilibili.com' or not re.fullmatch(r'/[1-9]\d{0,19}(?:/video)?/?', parsed.path):
            raise ProgramError('请输入 B 站 UP 主空间链接，例如 https://space.bilibili.com/12345/video 。')
        return 'https://space.bilibili.com/' + parsed.path.strip('/').split('/')[0] + '/video'
    raise ProgramError('视频来源类型无效。')


def inspect_bilibili_video(url, max_minutes):
    parsed = urlsplit(check_public_url(url))
    if parsed.hostname not in {'www.bilibili.com', 'bilibili.com'}:
        raise ProgramError('请输入 B 站公开视频链接。')
    match = re.fullmatch(r'/video/(BV[A-Za-z0-9]{10})/?', parsed.path)
    if not match:
        raise ProgramError('B 站视频链接缺少有效 BV 编号。')
    video_id = match.group(1)
    canonical = 'https://www.bilibili.com/video/' + video_id
    try:
        info = json.loads(_yt_command(['--dump-single-json', '--skip-download', canonical],
                                      timeout=80, use_proxy=False))
    except (ProgramError, ValueError) as exc:
        raise ProgramError('无法核对这条 B 站公开视频，未加入处理队列。') from exc
    uploader_id = str(info.get('uploader_id') or '')
    duration = math.ceil(float(info.get('duration') or 0))
    if (not re.fullmatch(r'[1-9]\d{0,19}', uploader_id) or info.get('id') != video_id
            or info.get('availability') not in (None, 'public') or info.get('is_live')
            or not 0 < duration <= max_minutes * 60):
        raise ProgramError('视频身份、公开状态或时长不符合处理要求。')
    date = str(info.get('upload_date') or '')
    return {'external_id': video_id, 'url': canonical, 'title': str(info.get('title') or '')[:500],
            'duration_seconds': duration, 'published_at':
            datetime.strptime(date, '%Y%m%d').replace(tzinfo=datetime_timezone.utc)
            if re.fullmatch(r'\d{8}', date) else None,
            'channel_id': uploader_id, 'channel_name': str(info.get('uploader') or 'B 站 UP 主')[:160]}


def _video_listing(kind, source_url, *, count=12):
    args = ['--yes-playlist', '--flat-playlist', '--playlist-end', str(count),
            '--dump-single-json', '--skip-download', source_url]
    try:
        listing = json.loads(_yt_command(args, timeout=80, use_proxy=kind == 'youtube'))
    except (ValueError, ProgramError) as exc:
        raise ProgramError('无法读取公开视频列表，请检查地址和平台访问情况。') from exc
    entries = listing.get('entries')
    if not isinstance(entries, list) or not entries:
        raise ProgramError('该地址未返回公开视频。')
    if kind == 'youtube':
        playlist = parse_qs(urlsplit(source_url).query).get('list', [''])[0]
        if playlist and str(listing.get('id') or '') != playlist:
            raise ProgramError('YouTube 播放列表编号与返回内容不一致。')
        channel_id = str(listing.get('channel_id') or '')
        if not playlist and not re.fullmatch(r'UC[A-Za-z0-9_-]{22}', channel_id):
            raise ProgramError('无法核对 YouTube 频道身份。')
        if playlist:
            channel_id = ''
    else:
        channel_id = source_url.split('/')[3]
    return listing, channel_id


def parse_custom_rss(body, kind, feed_url):
    try:
        root = _safe_xml_root(body)
        channel = _children(root, 'channel')
        entries = _children(channel[0], 'item') if channel else _children(root, 'entry')
    except Exception as exc:
        raise ProgramError('订阅地址未返回可读取的 RSS 或 Atom。') from exc
    if not entries:
        raise ProgramError('订阅暂未返回文章或节目。')
    result = []
    for node in entries[:30]:
        title = _first_text(node, 'title')[:500]
        link = _entry_link(node, feed_url)
        if not title or not link:
            continue
        try:
            link = check_public_url(link)
        except ProgramError:
            continue
        enclosures = _children(node, 'enclosure')
        audio = next((enclosure.get('url', '') for enclosure in enclosures
                      if (enclosure.get('type', '').startswith('audio/') or
                          enclosure.get('url', '').lower().split('?')[0].endswith(('.mp3', '.m4a')))), '')
        if kind == 'podcast':
            if not audio:
                continue
            try:
                audio = check_public_url(audio)
            except ProgramError:
                continue
        else:
            audio = ''
        content = _first_text(node, 'encoded', 'content')
        segments = plain_segments(content) if kind == 'article' and len(content) > 1500 else []
        published = _parse_datetime(_first_text(node, 'pubdate', 'published', 'updated'))
        external = _first_text(node, 'guid', 'id') or link
        result.append({'external_id': hashlib.sha256(external.encode()).hexdigest(),
                       'title': title, 'url': link, 'audio_url': audio,
                       'duration_seconds': duration_seconds(_first_text(node, 'duration')),
                       'published_at': published, 'segments': segments})
    if not result:
        raise ProgramError('订阅中没有可用的公开内容。')
    return result


def video_items(subscription, *, count=12):
    spec = source_spec(subscription)
    kind = spec['kind']
    listing, channel_id = _video_listing(kind, spec['url'], count=count)
    if subscription.playlist_id and str(listing.get('id') or '') != subscription.playlist_id:
        raise ProgramError('公开视频列表与已保存的播放列表不一致。')
    if subscription.channel_id and channel_id != subscription.channel_id:
        raise ProgramError('视频列表的频道身份与保存的订阅不一致。')
    result = []
    for row in listing['entries'][:count]:
        video_id = str(row.get('id') or '')
        if kind == 'youtube' and not re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id):
            continue
        if kind == 'bilibili' and not re.fullmatch(r'BV[A-Za-z0-9]{10}', video_id):
            continue
        title = str(row.get('title') or '')[:500]
        if not title:
            continue
        stamp = row.get('timestamp') or row.get('release_timestamp')
        date = str(row.get('upload_date') or '')
        published = (datetime.fromtimestamp(stamp, tz=datetime_timezone.utc) if stamp else
                     datetime.strptime(date, '%Y%m%d').replace(tzinfo=datetime_timezone.utc)
                     if re.fullmatch(r'\d{8}', date) else None)
        url = ('https://www.youtube.com/watch?v=' if kind == 'youtube' else 'https://www.bilibili.com/video/') + video_id
        duration = math.ceil(float(row.get('duration') or 0))
        if (subscription.min_duration_seconds and not duration) or (subscription.publish_weekday is not None and not published):
            from types import SimpleNamespace
            from .program_media import youtube_metadata, bilibili_metadata
            entry = SimpleNamespace(subscription=subscription, external_id=video_id, url=url)
            info = (youtube_metadata(entry, 1440, timeout=60) if kind == 'youtube'
                    else bilibili_metadata(entry, 1440))
            duration = math.ceil(float(info.get('duration') or 0))
            stamp = info.get('timestamp') or info.get('release_timestamp')
            date = str(info.get('upload_date') or '')
            published = (datetime.fromtimestamp(stamp, tz=datetime_timezone.utc) if stamp else
                         datetime.strptime(date, '%Y%m%d').replace(tzinfo=datetime_timezone.utc)
                         if re.fullmatch(r'\d{8}', date) else None)
        result.append({'external_id': video_id, 'title': title, 'url': url,
                       'duration_seconds': duration, 'published_at': published})
    if not result:
        raise ProgramError('视频列表中没有可用的公开视频。')
    return result


def inspect_new_source(kind, url, *, needs_details=False):
    if kind in {'youtube', 'bilibili'}:
        canonical = video_source_url(kind, url)
        listing, channel_id = _video_listing(kind, canonical)
        playlist_id = parse_qs(urlsplit(canonical).query).get('list', [''])[0]
        # Build a temporary subscription with exactly the same validated parser as the worker.
        from types import SimpleNamespace
        sample = SimpleNamespace(kind=kind, code='custom', custom_name=str(listing.get('title') or '')[:160],
            source_url=canonical, feed_url='', channel_id=channel_id, playlist_id=playlist_id,
            min_duration_seconds=1 if needs_details else 0, publish_weekday=0 if needs_details else None)
        return {'name': sample.custom_name or ('YouTube 频道' if kind == 'youtube' else 'B 站 UP 主'),
                'source_url': canonical, 'feed_url': '', 'channel_id': channel_id, 'playlist_id': playlist_id,
                'items': video_items(sample, count=12)}
    if kind in {'podcast', 'article'}:
        feed_url = check_public_url(url)
        try:
            response = fetch_user_source_url(feed_url, max_bytes=8000000)
        except SafeHttpError as exc:
            raise ProgramError(exc.safe_message) from exc
        items = parse_custom_rss(response.body, kind, feed_url)
        return {'name': urlsplit(feed_url).hostname, 'source_url': feed_url,
                'feed_url': feed_url, 'channel_id': '', 'playlist_id': '', 'items': items}
    raise ProgramError('请选择支持的公开信源类型。')


def matches_filters(subscription, item):
    title = str(item.get('title') or '').casefold()
    include = [x.strip().casefold() for x in re.split(r'[,，\n]', subscription.include_terms) if x.strip()]
    exclude = [x.strip().casefold() for x in re.split(r'[,，\n]', subscription.exclude_terms) if x.strip()]
    if include and not (all(term in title for term in include) if subscription.include_mode == 'all'
                        else any(term in title for term in include)):
        return False
    if any(term in title for term in exclude):
        return False
    published = item.get('published_at')
    if subscription.publish_weekday is not None:
        if not published or timezone.localtime(published).weekday() != subscription.publish_weekday:
            return False
    if subscription.min_duration_seconds:
        if not item.get('duration_seconds') or item['duration_seconds'] < subscription.min_duration_seconds:
            return False
    return True


def fetch_custom_article(entry):
    try:
        response = fetch_user_source_url(entry.url, max_bytes=5000000)
    except SafeHttpError as exc:
        raise ProgramError(exc.safe_message) from exc
    soup = BeautifulSoup(response.body, 'html.parser')
    for node in soup.select('script,style,nav,footer,aside,form,header,iframe'):
        node.decompose()
    target = soup.select_one('article') or soup.select_one('main')
    if not target:
        raise ProgramError('文章页未找到稳定正文；可手动上传文字稿。')
    segments = plain_segments(str(target))
    if sum(len(s['text']) for s in segments) < 500:
        raise ProgramError('文章正文过短，可能需要登录或只有摘要；可手动上传文字稿。')
    return segments
