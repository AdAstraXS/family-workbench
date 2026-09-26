import io
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

from .http_client import fetch_public_url, validate_public_http_url
from .program_sources import CATALOGUE, ProgramError
from knowledge.crypto import decrypt_json

MAX_AUDIO_BYTES = 60 * 1024 * 1024
ASR_MODEL = 'fun-asr-2025-11-07'


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


def asr_request(config, *, task_id='', audio_url=''):
    key = decrypt_json(config.encrypted_credentials).get('api_key', '')
    if not config.allow_asr or not key:
        raise ProgramError('转写尚未开启，管理员需配置百炼 Key 并授权发送公开节目音频。')
    workspace = config.workspace_id.strip()
    if workspace and not re.fullmatch(r'[a-zA-Z0-9-]{1,80}', workspace):
        raise ProgramError('百炼 Workspace ID 格式不正确。')
    host = f'{workspace}.cn-beijing.maas.aliyuncs.com' if workspace else 'dashscope.aliyuncs.com'
    base = f'https://{host}/api/v1'
    if task_id:
        if not re.fullmatch(r'[a-zA-Z0-9-]{1,150}', task_id):
            raise ProgramError('转写任务 ID 格式不正确。')
        return private_json_request(base + '/tasks/' + task_id, key=key)
    parsed_audio = urlsplit(audio_url)
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


def _yt_command(args, timeout=180):
    try:
        completed = subprocess.run([sys.executable, '-m', 'yt_dlp', '--ignore-config', '--no-playlist',
            '--no-warnings', '--socket-timeout', '20', '--retries', '1', *args],
            capture_output=True, timeout=timeout, check=True)
        return completed.stdout
    except (subprocess.SubprocessError, OSError) as exc:
        raise ProgramError('YouTube 获取失败，可能是网络或平台访问限制；可导入已有字幕或音频后继续。') from exc


def youtube_metadata(entry, max_minutes):
    if entry.subscription.code != 'rhino' or not re.fullmatch(r'[A-Za-z0-9_-]{11}', entry.external_id):
        raise ProgramError('当前只支持已订阅的视野环球财经公开视频。')
    url = 'https://www.youtube.com/watch?v=' + entry.external_id
    info = json.loads(_yt_command(['--dump-single-json', '--skip-download', url]))
    if info.get('channel_id') != CATALOGUE['rhino']['channel_id']:
        raise ProgramError('视频不属于已批准的频道。')
    if info.get('availability') not in (None, 'public') or info.get('is_live') or info.get('live_status') == 'is_upcoming':
        raise ProgramError('只支持公开且已经结束的节目。')
    duration = int(info.get('duration') or 0)
    if not 0 < duration <= max_minutes * 60:
        raise ProgramError('节目时长未知或超过单集上限。')
    return info


def youtube_captions(info):
    for captions in (info.get('subtitles', {}), info.get('automatic_captions', {})):
        for language in ('zh-Hans', 'zh-CN', 'zh', 'zh-Hant', 'en'):
            for variant in captions.get(language, []):
                url = variant.get('url', '')
                if variant.get('ext') != 'json3' or (urlsplit(url).hostname or '') not in {'www.youtube.com', 'youtube.com'}:
                    continue
                try:
                    data = json.loads(fetch_public_url(url, max_bytes=5000000).body)
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
