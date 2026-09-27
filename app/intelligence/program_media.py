import io
import json
import math
import re
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from datetime import datetime, timezone
from types import SimpleNamespace
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

from tinytag import TinyTag

from .http_client import fetch_public_url, validate_public_http_url
from .program_network import fetch_source_url, source_proxy
from .program_sources import CATALOGUE, ProgramError, ProgramConfigurationRequired
from knowledge.crypto import decrypt_json

MAX_AUDIO_BYTES = 60 * 1024 * 1024
ASR_MODEL = 'fun-asr-2025-11-07'


def media_duration(body):
    try:
        seconds = float(TinyTag.get(file_obj=io.BytesIO(body)).duration)
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError
        return math.ceil(seconds)
    except Exception as exc:
        raise ProgramError('无法核对音视频时长，未发送至转写服务。') from exc


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def private_json_request(url, *, key, payload=None, headers=None):
    """Do not follow redirects carrying credentials; never expose vendor error bodies."""
    validate_public_http_url(url)
    data = json.dumps(payload).encode() if payload is not None else None
    request = Request(url, data=data, headers={
        'Authorization': f'Bearer {key}', 'Content-Type': 'application/json', **(headers or {})})
    try:
        with build_opener(NoRedirect()).open(request, timeout=90) as response:
            body = response.read(4000001)
        if len(body) > 4000000:
            raise ValueError('size')
        return json.loads(body)
    except Exception as exc:
        raise ProgramError('模型服务请求失败，请核对 Key、额度和网络；未自动重新提交付费请求。') from exc


def _asr_access(config):
    key = decrypt_json(config.encrypted_credentials).get('api_key', '')
    if not config.allow_asr or not key:
        raise ProgramConfigurationRequired('转写尚未开启，管理员需配置百炼 Key 并授权发送公开节目音频。')
    workspace = config.workspace_id.strip()
    if workspace and not re.fullmatch(r'[a-zA-Z0-9-]{1,80}', workspace):
        raise ProgramError('百炼 Workspace ID 格式不正确。')
    host = f'{workspace}.cn-beijing.maas.aliyuncs.com' if workspace else 'dashscope.aliyuncs.com'
    base = f'https://{host}/api/v1'
    return key, base


def upload_asr_audio(config, body, mime):
    """Upload public audio using a model-bound, private 48-hour Bailian lease."""
    suffixes = {'audio/mp4': '.m4a', 'audio/mpeg': '.mp3', 'video/mp4': '.mp4',
                'audio/wav': '.wav', 'audio/webm': '.webm'}
    if not body or len(body) > MAX_AUDIO_BYTES or mime not in suffixes:
        raise ProgramError('音频为空、格式异常或超过传输上限。')
    key, base = _asr_access(config)
    policy = private_json_request(base + '/uploads?action=getPolicy&model=' + ASR_MODEL, key=key).get('data', {})
    host = str(policy.get('upload_host', ''))
    parsed = urlsplit(host)
    if (parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port not in (None, 443)
            or not (parsed.hostname or '').endswith('.oss-cn-beijing.aliyuncs.com')):
        raise ProgramError('百炼临时存储地址异常，音频未上传。')
    validate_public_http_url(host)
    directory = str(policy.get('upload_dir', ''))
    if not re.fullmatch(r'dashscope-instant/[A-Za-z0-9_/-]+', directory) or '..' in directory:
        raise ProgramError('百炼临时存储路径异常，音频未上传。')
    if len(body) > int(policy.get('max_file_size_mb', 0)) * 1024 * 1024:
        raise ProgramError('音频超过百炼上传凭证的大小限制。')
    filename = uuid.uuid4().hex + suffixes[mime]
    object_key = directory.rstrip('/') + '/' + filename
    fields = {'OSSAccessKeyId': policy.get('oss_access_key_id'), 'Signature': policy.get('signature'),
        'policy': policy.get('policy'), 'x-oss-object-acl': policy.get('x_oss_object_acl'),
        'x-oss-forbid-overwrite': policy.get('x_oss_forbid_overwrite'), 'key': object_key, 'success_action_status': '200'}
    if fields['x-oss-object-acl'] != 'private' or any(not isinstance(v, str) or not v for v in fields.values()):
        raise ProgramError('百炼未返回有效的私有上传凭证。')
    boundary = 'program-' + uuid.uuid4().hex
    parts = []
    for name, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    parts.extend([f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\nContent-Type: {mime}\r\n\r\n'.encode(),
                  body, f'\r\n--{boundary}--\r\n'.encode()])
    request = Request(host, data=b''.join(parts), headers={'Content-Type': 'multipart/form-data; boundary=' + boundary})
    try:
        with build_opener(NoRedirect()).open(request, timeout=180) as response:
            if response.status != 200:
                raise ValueError('upload status')
    except Exception as exc:
        raise ProgramError('音频上传百炼临时存储失败，尚未提交转写。') from exc
    return 'oss://' + object_key


def podcast_audio(entry):
    if entry.subscription.code == 'good_company':
        if urlsplit(entry.audio_url).hostname != 'sphinx.acast.com':
            raise ProgramError('播客音频地址不属于已批准的出版方。')
        body = fetch_source_url(entry.audio_url, max_bytes=MAX_AUDIO_BYTES, timeout=90).body
    elif entry.subscription.kind == 'podcast':
        from .program_network import fetch_user_source_url
        body = fetch_user_source_url(entry.audio_url, max_bytes=MAX_AUDIO_BYTES, timeout=90).body
    else:
        raise ProgramError('该来源没有可用的播客音频。')
    if body.startswith(b'ID3') or (len(body) > 2 and body[0] == 255 and body[1] & 224 == 224):
        return body, 'audio/mpeg'
    if len(body) > 12 and body[4:8] == b'ftyp':
        return body, 'audio/mp4'
    raise ProgramError('出版方未返回有效的 MP3/M4A 音频，未上传或计费。')


def asr_request(config, *, task_id='', audio_url=''):
    key, base = _asr_access(config)
    if task_id:
        if not re.fullmatch(r'[a-zA-Z0-9-]{1,150}', task_id):
            raise ProgramError('转写任务 ID 格式不正确。')
        return private_json_request(base + '/tasks/' + task_id, key=key)
    parsed_audio = urlsplit(audio_url)
    if audio_url.startswith('oss://dashscope-instant/') and not parsed_audio.query and not parsed_audio.fragment and '..' not in audio_url:
        return private_json_request(base + '/services/audio/asr/transcription', key=key,
            headers={'X-DashScope-Async': 'enable', 'X-DashScope-OssResourceResolve': 'enable'},
            payload={'model': ASR_MODEL, 'input': {'file_urls': [audio_url]}, 'parameters': {'channel_id': [0]}})
    if parsed_audio.scheme != 'https' or parsed_audio.username or parsed_audio.password:
        raise ProgramError('音频传输必须使用 HTTPS，且不包含账号密码。')
    # The approved NAS endpoint uses HTTPS on 8443. It is passed to the ASR service,
    # never fetched by this server; keep DNS/public-host validation without rejecting that port.
    if audio_url.startswith(config.public_base_url.rstrip('/') + '/intelligence/programs/') and config.public_base_url:
        validate_public_http_url(urlunsplit(('https', parsed_audio.hostname, '/', '', '')))
    else:
        validate_public_http_url(audio_url)
    return private_json_request(base + '/services/audio/asr/transcription', key=key,
        headers={'X-DashScope-Async': 'enable'}, payload={'model': ASR_MODEL,
        'input': {'file_urls': [audio_url]}, 'parameters': {'channel_id': [0]}})


def parse_asr_result(result):
    channels = result.get('transcripts', [])
    if not channels or not isinstance(channels, list):
        raise ProgramError('转写服务未返回文字稿。')
    sentences = channels[0].get('sentences', [])
    segments = []
    for item in sentences:
        start, end, text = item.get('begin_time'), item.get('end_time'), str(item.get('text', '')).strip()
        if not text or type(start) is not int or type(end) is not int or not 0 <= start <= end <= 86400000:
            raise ProgramError('转写时间戳或段落无效。')
        segments.append({'text': text, 'start_ms': start, 'end_ms': end})
    if not segments:
        raise ProgramError('转写结果缺少可定位的句子，请核对音频。')
    return segments


def download_asr_result(output):
    items = output.get('results', [])
    if len(items) != 1 or items[0].get('subtask_status') != 'SUCCEEDED':
        raise ProgramError('音频转写子任务失败，请在百炼控制台核对任务。')
    url = items[0].get('transcription_url', '')
    host = urlsplit(url).hostname or ''
    if not host.endswith('.aliyuncs.com') or urlsplit(url).scheme != 'https':
        raise ProgramError('转写结果下载地址异常。')
    body = fetch_public_url(url, max_bytes=8000000).body
    return parse_asr_result(json.loads(body))


def _yt_command(args, timeout=180, *, use_proxy=True):
    proxy_args = ['--proxy', source_proxy()] if use_proxy and source_proxy() else []
    try:
        completed = subprocess.run([sys.executable, '-m', 'yt_dlp', '--ignore-config', '--no-playlist',
            '--no-warnings', '--socket-timeout', '20', '--retries', '1', *proxy_args, *args],
            capture_output=True, timeout=timeout, check=True)
        return completed.stdout
    except (subprocess.SubprocessError, OSError) as exc:
        raise ProgramError('YouTube 获取失败，可能是网络或平台访问限制；可导入已有字幕或音频后继续。') from exc


def youtube_metadata(entry, max_minutes, *, timeout=180):
    from .program_custom_sources import source_spec
    spec = source_spec(entry.subscription)
    if spec['kind'] != 'youtube' or not re.fullmatch(r'[A-Za-z0-9_-]{11}', entry.external_id):
        raise ProgramError('当前只支持已订阅的 YouTube 公开视频。')
    url = 'https://www.youtube.com/watch?v=' + entry.external_id
    info = json.loads(_yt_command(['--dump-single-json', '--skip-download', url], timeout=timeout))
    if spec['channel_id'] and info.get('channel_id') != spec['channel_id']:
        raise ProgramError('视频不属于已批准的频道。')
    if info.get('availability') not in (None, 'public') or info.get('is_live') or info.get('live_status') == 'is_upcoming':
        raise ProgramError('只支持公开且已经结束的节目。')
    duration = math.ceil(float(info.get('duration') or 0))
    if not 0 < duration <= max_minutes * 60:
        raise ProgramError('节目时长未知或超过单集上限。')
    return info


def youtube_recent_entries():
    """Metadata-only fallback for transient failure of the public Atom feed."""
    spec = CATALOGUE['rhino']
    listing = json.loads(_yt_command(['--yes-playlist', '--flat-playlist', '--playlist-end', '3',
        '--dump-single-json', '--skip-download', spec['url'] + '/videos'], timeout=45))
    if listing.get('channel_id') != spec['channel_id']:
        raise ProgramError('节目列表不属于已批准的频道。')
    result = []
    for item in listing.get('entries', [])[:3]:
        video_id = item.get('id', '')
        if not re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id):
            raise ProgramError('节目列表包含无效视频标识。')
        entry = SimpleNamespace(subscription=SimpleNamespace(code='rhino'), external_id=video_id)
        info = youtube_metadata(entry, 1440, timeout=45)
        date = str(info.get('upload_date') or '')
        if not re.fullmatch(r'\d{8}', date):
            raise ProgramError('节目列表缺少发布日期，暂不自动入队。')
        result.append({'external_id': video_id, 'title': str(info.get('title') or '')[:500],
            'url': 'https://www.youtube.com/watch?v=' + video_id,
            'duration_seconds': int(info['duration']),
            'published_at': datetime.strptime(date, '%Y%m%d').replace(tzinfo=timezone.utc)})
    if not result:
        raise ProgramError('官方频道暂未返回节目列表。')
    return result


def youtube_captions(info):
    for captions in (info.get('subtitles', {}), info.get('automatic_captions', {})):
        for language in ('zh-Hans', 'zh-CN', 'zh', 'zh-Hant', 'en'):
            for variant in captions.get(language, []):
                url = variant.get('url', '')
                if variant.get('ext') != 'json3' or (urlsplit(url).hostname or '') not in {'www.youtube.com', 'youtube.com'}:
                    continue
                try:
                    data = json.loads(fetch_source_url(url, max_bytes=5000000).body)
                    result = []
                    for event in data.get('events', []):
                        text = ''.join(s.get('utf8', '') for s in event.get('segs', [])).strip()
                        if text:
                            start = int(event.get('tStartMs', 0))
                            result.append({'text': text, 'start_ms': start, 'end_ms': start + int(event.get('dDurationMs', 0))})
                    if result:
                        return result
                except Exception:
                    continue
    return []


def youtube_audio(entry):
    with tempfile.TemporaryDirectory(prefix='intelligence-audio-') as directory:
        path = Path(directory) / 'audio.m4a'
        _yt_command(['-f', 'bestaudio[ext=m4a]', '--max-filesize', str(MAX_AUDIO_BYTES),
                     '--fragment-retries', '1', '--no-progress', '-o', str(path),
                     'https://www.youtube.com/watch?v=' + entry.external_id], timeout=300)
        if not path.exists() or not 0 < path.stat().st_size <= MAX_AUDIO_BYTES:
            raise ProgramError('未取得音频，或音频超过 60 MB 上限。')
        return path.read_bytes(), 'audio/mp4'


def bilibili_metadata(entry, max_minutes):
    if entry.subscription.kind != 'bilibili' or not re.fullmatch(r'BV[A-Za-z0-9]{10}', entry.external_id):
        raise ProgramError('B 站视频编号无效。')
    info = json.loads(_yt_command(['--dump-single-json', '--skip-download', entry.url],
                                  timeout=90, use_proxy=False))
    if str(info.get('uploader_id') or '') != entry.subscription.channel_id:
        raise ProgramError('B 站视频不属于已订阅的 UP 主。')
    if info.get('availability') not in (None, 'public') or info.get('is_live'):
        raise ProgramError('只支持已经发布的公开视频。')
    duration = math.ceil(float(info.get('duration') or 0))
    if not 0 < duration <= max_minutes * 60:
        raise ProgramError('节目时长未知或超过单集上限。')
    return info


def bilibili_audio(entry):
    with tempfile.TemporaryDirectory(prefix='intelligence-bili-') as directory:
        path = Path(directory) / 'audio.m4a'
        _yt_command(['-f', 'bestaudio[ext=m4a]', '--max-filesize', str(MAX_AUDIO_BYTES),
                     '--fragment-retries', '1', '--no-progress', '-o', str(path), entry.url],
                    timeout=300, use_proxy=False)
        if not path.exists() or not 0 < path.stat().st_size <= MAX_AUDIO_BYTES:
            raise ProgramError('未取得 B 站音频，或音频超过 60 MB 上限。')
        body = path.read_bytes()
        if body[4:8] != b'ftyp':
            raise ProgramError('B 站返回的音频格式不受支持。')
        return body, 'audio/mp4'
