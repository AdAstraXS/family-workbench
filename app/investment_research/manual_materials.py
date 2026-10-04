"""Explicit uploads/public links; keep the original even if extraction fails."""
import gzip
import hashlib
import re
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit, parse_qsl
from django.utils.dateparse import parse_date
from knowledge.web_fetch import canonical_url, public_request, WebCaptureError
from .ir_extraction import extract_material
from .models import ResearchSupplement
from .preparation import authorize
from .research_ai import ResearchAiError

MAX_BYTES = 8 * 1024 * 1024


def add(actor, dossier, values, uploaded=None):
    authorize(actor, dossier)
    title = str(values.get('title', '')).strip()
    period = str(values.get('period', '')).strip()
    summary = str(values.get('summary', '')).strip()
    if not title or len(title) > 500 or len(period) > 100 or len(summary) > 1000:
        raise ResearchAiError('请填写名称；名称、报告期、简介分别不超过500、100、1000字。')
    published = values.get('published_at', '')
    try:
        date = parse_date(published) if published else None
    except ValueError as exc:
        raise ResearchAiError('发布日期无效。') from exc
    if published and not date:
        raise ResearchAiError('发布日期无效。')
    url = str(values.get('url', '')).strip()
    if bool(uploaded) == bool(url):
        raise ResearchAiError('请选择上传文件或补充公开链接其中一种方式。')
    if uploaded:
        if uploaded.size > MAX_BYTES:
            raise ResearchAiError('文件不超过8 MB。')
        raw = uploaded.read(MAX_BYTES + 1)
        name = Path(uploaded.name.replace('\\', '/')).name[:250]
        media = str(uploaded.content_type or 'application/octet-stream').split(';')[0]
        extraction_url = 'https://uploaded.invalid/' + name
    else:
        if values.get('public_link') != 'yes':
            raise ResearchAiError('请确认链接公开可访问且不包含凭据。')
        try:
            url = canonical_url(url)
            if any(re.search(r'token|password|secret|signature|api.?key|auth', k, re.I) for k, _ in parse_qsl(urlsplit(url).query)):
                raise WebCaptureError('链接可能包含访问凭据，请改用不含凭据的公开链接。')
            raw, media = public_request(url, limit=MAX_BYTES, timeout=20)
        except WebCaptureError as exc:
            raise ResearchAiError(str(exc)) from exc
        name = Path(urlsplit(url).path).name[:250]
        media = media.split(';')[0].strip()
        extraction_url = url
    if not raw or len(raw) > MAX_BYTES:
        raise ResearchAiError('文件为空或超过8 MB。')
    sha = hashlib.sha256(raw).hexdigest()
    existing = dossier.supplements.filter(sha256=sha).first()
    if existing:
        return existing, False
    text, note = '', ''
    try:
        if media in {'text/plain', 'text/markdown'} or Path(name).suffix.lower() in {'.txt', '.md'}:
            text = raw.decode('utf-8-sig')
            if len(text) > 1_000_000:
                raise ValueError
            media = 'text/plain'
        else:
            extracted = extract_material(SimpleNamespace(url=extraction_url, raw=raw, content_type=media))
            text, media = extracted['text'], extracted['media_type']
        if not text.strip():
            raise ValueError
        note = '正文已提取；内容及口径仍需核对。'
    except Exception:
        text, note = '', '原件已保留，正文提取失败或格式暂不支持；请下载核对。'
    row, created = ResearchSupplement.objects.get_or_create(dossier=dossier, sha256=sha, defaults={
        'created_by': actor, 'title': title, 'source_url': url, 'original_name': name,
        'media_type': media[:120], 'raw_gzip': gzip.compress(raw), 'text': text,
        'period': period, 'published_at': date, 'summary': summary, 'extraction_note': note})
    return row, created
