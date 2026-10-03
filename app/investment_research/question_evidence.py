"""Saved source packets, scoped to one private dossier; no network on reads."""
import hashlib
import json
from django.urls import reverse
from .preparation import packet, _narrative, _user_prompt
from .research_ai import ResearchAiError


def append(content, source, pieces, ceiling):
    for text, offset in pieces:
        text = text[:1800]
        item = {**source, 'id': f'E{len(content["evidence"]) + 1}', 'text': text, 'offset': offset,
                'excerpt_sha256': hashlib.sha256(text.encode()).hexdigest()}
        content['evidence'].append(item)
        if len(_user_prompt(content)) > ceiling:
            content['evidence'].pop()
            break


def add_supplements(dossier, content, ceiling):
    for row in dossier.supplements.exclude(text='').defer('raw_gzip').order_by('-pk')[:12]:
        append(content, {'title': row.title, 'kind': 'supplement', 'supplement_id': row.pk,
            'date': row.period or str(row.published_at or '报告期未标注'), 'sha256': row.sha256,
            'fetched_at': row.created_at.isoformat(), 'url': reverse('investment_research:supplement_read', args=[dossier.pk, row.pk]),
            'source_note': '本人补充资料；出处及口径仍需核查'}, _narrative(row.text)[:4] or [(row.text[:1800], 0)], ceiling)


def build(dossier, ceiling):
    content = packet(dossier, max(2200, ceiling // 2), include_personal=False, allow_empty=True)
    add_supplements(dossier, content, ceiling * 3 // 4)
    from investment_watch.models import ResearchCandidate, BodySnapshot, BodyAttempt
    seen = set()
    candidates = ResearchCandidate.objects.filter(dossier=dossier, material_version__material__source__family=dossier.family).select_related('material_version').order_by('-material_version__found_at')[:20]
    for candidate in candidates:
        row = candidate.material_version
        if row.pk in seen:
            continue
        seen.add(row.pk)
        body = BodySnapshot.objects.filter(material_version=row).first()
        attempt = BodyAttempt.objects.filter(family=dossier.family, security=dossier.security, material_version=row).first()
        text = body.text if body else row.summary
        if not text:
            continue
        source = {'title': row.title, 'kind': 'news_body' if body else 'news_lead', 'news_version_id': row.pk,
            'date': str(row.published_at or '发布日期未知'), 'sha256': body.content_hash if body else row.content_hash,
            'fetched_at': str(body.fetched_at if body else row.found_at),
            'url': reverse('investment_research:question_news_source', args=[dossier.pk, row.pk]),
            'source_note': ('已存新闻正文摘录' if body else '仅标题与摘要线索，未读正文，不能据此验证指标或事实') + f'；资料状态：{row.status}' +
                           ('；正文获取：' + attempt.message if attempt else '')}
        append(content, source, _narrative(text)[:2] or [(text[:1800], 0)], ceiling)
    if not content['evidence']:
        raise ResearchAiError('尚无可分析正文或新闻线索，请先获取或补充资料。')
    content.pop('existing_judgment', None)
    return content


def fingerprint(content):
    sources = sorted((e.get('kind', ''), str(e.get('version_id') or e.get('official_version_id') or e.get('supplement_id') or e.get('news_version_id') or ''), e.get('sha256', '')) for e in content['evidence'])
    return hashlib.sha256(json.dumps(sources, ensure_ascii=False).encode()).hexdigest()
