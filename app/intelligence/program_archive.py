import hashlib
import json
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import Max
from django.urls import reverse
from django.utils.html import escape
from knowledge.models import KnowledgeDocument, KnowledgeSource, KnowledgeRevision, KnowledgeVisibility
from knowledge.search import index_document
from .program_models import ProgramRevision
from .program_sources import ProgramError
from .program_custom_sources import source_spec
from .program_processing import validate_points


def archive_program(revision, member, *, include_summary=None, include_transcript=True):
    if revision.entry.subscription.family_id != member.family_id or member.role == 'viewer':
        raise ProgramError('无权保存这份资料。')
    if revision.entry.private_owner_id and revision.entry.private_owner_id != member.pk:
        raise ProgramError('只有上传者可以保存这份私人资料。')
    stored = None
    try:
        with transaction.atomic():
            revision = ProgramRevision.objects.select_for_update().select_related('entry__subscription').get(pk=revision.pk)
            entry = revision.entry
            visibility = KnowledgeVisibility.PRIVATE if entry.private_owner_id else KnowledgeVisibility.FAMILY
            if include_summary is None:
                include_summary = bool(revision.summary_complete and revision.summary.get('points'))
            if not include_summary and not include_transcript:
                raise ProgramError('请至少选择 AI 摘要或完整原文。')
            if include_summary and (not revision.summary_complete or not revision.summary.get('points')):
                raise ProgramError('AI 摘要尚未完成，暂时只能保存原文。')
            if include_summary:
                rows = {i: s['text'] for i, s in enumerate(revision.segments, 1)}
                for point in revision.summary['points']:
                    validate_points({'points': [point]}, set(rows), rows)
            detail_url = reverse('intelligence:program_detail', args=[entry.pk]) + f'?version={revision.pk}'
            included = [name for name, selected in [('summary', include_summary), ('transcript', include_transcript)] if selected]
            payload = {'schema': 'program-archive-v2', 'source_url': entry.url, 'origin': revision.origin,
                       'content_hash': revision.content_hash, 'program_detail_url': detail_url, 'included': included}
            if include_summary:
                payload['summary'] = revision.summary
            if include_transcript:
                payload['segments'] = revision.segments
            raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
            digest = hashlib.sha256(raw).hexdigest()
            document = None
            if revision.archived_document_id:
                document = KnowledgeDocument.objects.select_for_update().get(pk=revision.archived_document_id)
                if document.current_revision.content_hash == digest:
                    return document
                if document.owner_id != member.pk and member.role != 'admin':
                    raise ProgramError('这份知识资料由其他成员保存，更新整理结果需要所有者或管理员操作。')
            source, _ = KnowledgeSource.objects.get_or_create(family=member.family, key='intelligence:programs', defaults={
                'kind': KnowledgeSource.KIND_INTELLIGENCE, 'name': 'AI 情报 · 精选订阅',
                'visibility': KnowledgeVisibility.FAMILY, 'allow_cloud_ai': False,
                'status': KnowledgeSource.STATUS_ACTIVE, 'is_enabled': True})
            if document is None:
                document = KnowledgeDocument.objects.create(family=member.family, source=source, owner=member,
                external_id=f'intelligence-program-revision:{revision.pk}', title=entry.title,
                author=source_spec(entry.subscription)['name'], section_name='精选订阅', source_url=entry.url,
                visibility=visibility, sync_status=KnowledgeDocument.SYNC_AVAILABLE,
                curation_status=KnowledgeDocument.CURATION_INBOX, knowledge_status=KnowledgeDocument.KNOWLEDGE_PENDING,
                library_tier=KnowledgeDocument.LIBRARY_ARCHIVE, content_created_at=entry.published_at,
                content_modified_at=revision.created_at, hierarchy={'program_entry_id': entry.pk, 'program_revision_id': revision.pk})
            intro = (f'{entry.title}\n来源：{entry.url}\n对应原文版本：{revision.content_hash}\n'
                     f'在 AI 情报中查看原文：{detail_url}\n')
            plain = intro
            html = ('<h1>' + escape(entry.title) + '</h1><p>' + escape(intro) + '</p>'
                    + '<p><a href="' + escape(detail_url) + '">在 AI 情报中查看原文及引用</a></p>')
            if include_summary:
                summary_text = '\n'.join(f"[{p['topic']} / {p['kind']}] {p['text']}（段落 {', '.join(map(str, p['refs']))}）"
                                         for p in revision.summary['points'])
                plain += '\nAI 整理（未经人工复核）\n' + summary_text
                html += '<h2>AI 整理（未经人工复核）</h2>'
                html += ''.join('<p>' + escape(line) + '</p>' for line in summary_text.splitlines())
            if include_transcript:
                plain += '\n\n原文\n' + revision.text
                html += '<h2>原文</h2>'
                html += ''.join(f'<p id="segment-{i}"><strong>{i}. </strong>{escape(s["text"])}</p>' for i, s in enumerate(revision.segments, 1))
            number = (KnowledgeRevision.objects.filter(document=document).aggregate(n=Max('revision_number'))['n'] or 0) + 1
            saved = KnowledgeRevision.objects.create(document=document, revision_number=number,
                content_hash=digest, normalized_hash=hashlib.sha256(plain.encode()).hexdigest(),
                normalized_html=html, plain_text=plain, converter_version='intelligence-program-v1', source_modified_at=revision.updated_at)
            saved.raw_file.save(f'program-{revision.pk}.json', ContentFile(raw), save=True)
            stored = saved.raw_file
            document.current_revision = saved
            document.hierarchy = {**document.hierarchy, 'program_archive_included': included}
            document.save(update_fields=['current_revision', 'hierarchy', 'updated_at'])
            revision.archived_document = document
            revision.save(update_fields=['archived_document', 'updated_at'])
            index_document(document)
            return document
    except Exception:
        if stored:
            stored.delete(save=False)
        raise
