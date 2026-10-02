"""Forms and bounded member uploads for self-service intelligence sources."""
import hashlib
import io
import re
import uuid
from pathlib import Path

from cryptography.fernet import Fernet
from django import forms
from django.core.files.base import ContentFile
from django.utils import timezone

from knowledge.crypto import _fernet_key
from .program_custom_sources import check_reference_url
from .program_media import MAX_AUDIO_BYTES, media_duration
from .program_models import ProgramEntry, ProgramSubscription
from .program_processing import save_revision, store_audio
from .program_sources import CATALOGUE, CATALOGUE_FORM_KINDS, ProgramError


SOURCE_CHOICES = [('youtube', 'YouTube 公开频道 / 播放列表'),
                  ('bilibili', 'B 站公开 UP 主'), ('podcast', '播客 RSS'), ('article', '文章 RSS')]
WEEKDAYS = [('', '不限')] + [(str(i), name) for i, name in enumerate(
    ['周一', '周二', '周三', '周四', '周五', '周六', '周日'])]


class ProgramSourceForm(forms.Form):
    kind = forms.ChoiceField(label='来源类型', choices=SOURCE_CHOICES)
    url = forms.URLField(label='频道、播放列表或 RSS 地址', max_length=2000)
    name = forms.CharField(label='显示名称', max_length=160, required=False)
    include_terms = forms.CharField(label='标题包含', max_length=500, required=False,
                                    widget=forms.TextInput(attrs={'placeholder': '多个词用逗号分开'}))
    include_mode = forms.ChoiceField(label='关键词关系', choices=[('any', '命中任一'), ('all', '同时命中全部')])
    exclude_terms = forms.CharField(label='标题排除', max_length=500, required=False,
                                    widget=forms.TextInput(attrs={'placeholder': '预告, 精华'}))
    publish_weekday = forms.ChoiceField(label='发布星期', choices=WEEKDAYS, required=False,
                                        help_text='按北京时间计算；节目延期发布时可能被排除。')
    min_duration_minutes = forms.IntegerField(label='最短时长（分钟）', required=False, min_value=0,
                                              max_value=240, initial=0)
    auto_process = forms.BooleanField(label='发现新内容后自动生成摘要', required=False)

    def __init__(self, *args, source=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.source = source

    def clean(self):
        values = super().clean()
        for field in ('include_terms', 'exclude_terms'):
            terms = [x.strip() for x in re.split(r'[,，\n]', values.get(field, '')) if x.strip()]
            if len(terms) > 12 or any(len(term) > 60 for term in terms):
                self.add_error(field, '最多 12 个词或短语，每项不超过 60 字。')
        if values.get('min_duration_minutes') and values.get('kind') == 'article':
            self.add_error('min_duration_minutes', '文章没有节目时长，请清空这个条件。')
        if values.get('min_duration_minutes') and self.source and self.source.code == 'rhino' and \
                values.get('url', '').strip() == source_form_initial(self.source)['url']:
            self.add_error('min_duration_minutes', '这个频道的官方列表没有时长，暂不能按时长筛选。')
        return values


def source_filter_fields(values):
    return {'include_terms': values.get('include_terms', '').strip(),
            'include_mode': values.get('include_mode', 'any'),
            'exclude_terms': values.get('exclude_terms', '').strip(),
            'publish_weekday': int(values['publish_weekday']) if values.get('publish_weekday') else None,
            'min_duration_seconds': (values.get('min_duration_minutes') or 0) * 60,
            'auto_process': bool(values.get('auto_process'))}


def source_form_initial(subscription):
    catalogue = CATALOGUE.get(subscription.code, {})
    return {'kind': CATALOGUE_FORM_KINDS.get(subscription.code, subscription.kind),
            'url': subscription.source_url or catalogue.get('url', ''),
            'name': subscription.custom_name or catalogue.get('name', ''), 'include_terms': subscription.include_terms,
            'include_mode': subscription.include_mode, 'exclude_terms': subscription.exclude_terms,
            'publish_weekday': str(subscription.publish_weekday) if subscription.publish_weekday is not None else '',
            'min_duration_minutes': subscription.min_duration_seconds // 60,
            'auto_process': subscription.auto_process}


class ProgramUploadForm(forms.Form):
    title = forms.CharField(label='资料标题', max_length=500)
    source_url = forms.URLField(label='原始节目链接（可选）', max_length=2000, required=False)
    file = forms.FileField(label='文字稿或音视频文件', help_text='TXT、SRT、VTT ≤2 MB；MP3、M4A、MP4、WAV、WebM ≤60 MB。')
    allow_asr = forms.BooleanField(label='同意将本次音视频发送至阿里云百炼转写，并计入家庭月预算', required=False)
    allow_summary = forms.BooleanField(label='同意将本次文字稿发送至已配置的 AI 模型整理', required=False)

    def clean_source_url(self):
        value = self.cleaned_data.get('source_url', '')
        if not value:
            return ''
        try:
            return check_reference_url(value)
        except ProgramError as exc:
            raise forms.ValidationError(str(exc)) from exc

    def clean_file(self):
        file = self.cleaned_data['file']
        extension = Path(file.name).suffix.casefold()
        if extension not in {'.txt', '.srt', '.vtt', '.mp3', '.m4a', '.mp4', '.wav', '.webm'}:
            raise forms.ValidationError('文件格式不支持。')
        limit = 2 * 1024 * 1024 if extension in {'.txt', '.srt', '.vtt'} else MAX_AUDIO_BYTES
        if not 0 < file.size <= limit:
            raise forms.ValidationError('文件为空或超过该格式的大小上限。')
        return file

    def clean(self):
        data = super().clean()
        file = data.get('file')
        if file and Path(file.name).suffix.casefold() not in {'.txt', '.srt', '.vtt'} and not data.get('allow_asr'):
            self.add_error('allow_asr', '音视频转写需要本次明确授权。')
        return data


def _milliseconds(value):
    value = value.replace(',', '.')
    base, dot, fraction = value.partition('.')
    parts = base.split(':')
    if len(parts) not in (2, 3):
        raise ProgramError('字幕时间格式不正确。')
    try:
        total = (int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2]) if len(parts) == 3
                 else int(parts[0]) * 60 + int(parts[1]))
        ms = int(fraction.ljust(3, '0')[:3]) if dot else 0
    except ValueError as exc:
        raise ProgramError('字幕时间格式不正确。') from exc
    return total * 1000 + ms


def text_segments(body, extension):
    try:
        text = body.decode('utf-8-sig')
    except UnicodeDecodeError as exc:
        raise ProgramError('文字稿请保存为 UTF-8 编码。') from exc
    if extension == '.txt':
        segments = [{'text': line.strip(), 'start_ms': None, 'end_ms': None}
                    for line in text.splitlines() if line.strip()]
    else:
        segments = []
        for block in re.split(r'\r?\n\s*\r?\n', text):
            lines = [line.strip() for line in block.splitlines() if line.strip()]
            timing = next((i for i, line in enumerate(lines) if '-->' in line), None)
            if timing is None:
                continue
            left, right = lines[timing].split('-->', 1)
            start = _milliseconds(left.strip().split()[0])
            end = _milliseconds(right.strip().split()[0])
            content = ' '.join(lines[timing + 1:])
            if content:
                segments.append({'text': content, 'start_ms': start, 'end_ms': end})
    if not segments:
        raise ProgramError('文件中没有可读取的完整文字稿。')
    return segments


def media_mime(body, extension):
    if extension == '.mp3' and (body.startswith(b'ID3') or
                                (len(body) > 2 and body[0] == 255 and body[1] & 224 == 224)):
        return 'audio/mpeg'
    if extension in {'.m4a', '.mp4'} and body[4:8] == b'ftyp':
        return 'video/mp4' if extension == '.mp4' else 'audio/mp4'
    if extension == '.wav' and body.startswith(b'RIFF') and body[8:12] == b'WAVE':
        return 'audio/wav'
    if extension == '.webm' and body.startswith(b'\x1a\x45\xdf\xa3'):
        return 'audio/webm'
    raise ProgramError('文件内容与扩展名不匹配，未上传或计费。')


def save_member_upload(member, values, *, max_minutes):
    uploaded = values['file']
    body = uploaded.read(MAX_AUDIO_BYTES + 1)
    extension = Path(uploaded.name).suffix.casefold()
    if not body or len(body) > (2 * 1024 * 1024 if extension in {'.txt', '.srt', '.vtt'} else MAX_AUDIO_BYTES):
        raise ProgramError('上传文件为空或超过上限。')
    segments = text_segments(body, extension) if extension in {'.txt', '.srt', '.vtt'} else None
    mime = '' if segments is not None else media_mime(body, extension)
    duration = 0 if segments is not None else media_duration(body)
    if duration > max_minutes * 60:
        raise ProgramError('音视频时长超过单集上限。')
    code = f'upload_{member.pk}'
    sub, _ = ProgramSubscription.objects.get_or_create(family=member.family, code=code,
        defaults={'kind': 'upload', 'custom_name': '我的上传', 'enabled': True, 'auto_process': False})
    entry = ProgramEntry.objects.create(subscription=sub, private_owner=member,
        external_id=uuid.uuid4().hex, title=values['title'].strip(), url=values.get('source_url') or '',
        duration_seconds=duration, requested=bool(segments is None or values.get('allow_summary')),
        allow_cloud_asr=bool(values.get('allow_asr')), allow_cloud_summary=bool(values.get('allow_summary')))
    try:
        entry.uploaded_sha256 = hashlib.sha256(body).hexdigest()
        entry.uploaded_name = Path(uploaded.name).name[:255]
        if segments is not None:
            entry.uploaded_original.save(uuid.uuid4().hex + '.encrypted',
                ContentFile(Fernet(_fernet_key()).encrypt(body)), save=False)
            entry.save(update_fields=['uploaded_original', 'uploaded_sha256', 'uploaded_name', 'updated_at'])
            save_revision(entry, segments, origin='upload', source_url=entry.url)
            if not values.get('allow_summary'):
                ProgramEntry.objects.filter(pk=entry.pk).update(state='ready')
        else:
            store_audio(entry, body, mime)
            ProgramEntry.objects.filter(pk=entry.pk).update(uploaded_sha256=entry.uploaded_sha256,
                uploaded_name=entry.uploaded_name)
        return entry
    except Exception:
        if entry.uploaded_original:
            entry.uploaded_original.delete(save=False)
        if entry.audio_file:
            entry.audio_file.delete(save=False)
        entry.delete()
        raise
