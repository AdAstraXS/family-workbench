import hashlib
import json
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils.html import escape
from knowledge.models import KnowledgeDocument, KnowledgeSource, KnowledgeRevision, KnowledgeVisibility
from knowledge.search import index_document
from .program_models import ProgramRevision
from .program_sources import CATALOGUE, ProgramError


def archive_program(revision, member):
    if revision.entry.subscription.family_id != member.family_id or member.role == 'viewer':
        raise ProgramError('无权保存这份资料。')
    stored = None
    try:
        with transaction.atomic():
            revision = ProgramRevision.objects.select_for_update().select_related('entry__subscription').get(pk=revision.pk)
            if revision.archived_document_id:
                return revision.archived_document
            entry = revision.entry
            source, _ = KnowledgeSource.objects.get_or_create(family=member.family, key='intelligence:programs', defaults={
                'kind': KnowledgeSource.KIND_INTELLIGENCE, 'name': 'AI 情报 · 精选订阅',
                'visibility': KnowledgeVisibility.FAMILY, 'allow_cloud_ai': False,
                'status': KnowledgeSource.STATUS_ACTIVE, 'is_enabled': True})
            document = KnowledgeDocument.objects.create(family=member.family, source=source, owner=member,
                external_id=f'intelligence-program-revision:{revision.pk}', title=entry.title,
                author=CATALOGUE[entry.subscription.code]['name'], section_name='精选订阅', source_url=entry.url,
                visibility=KnowledgeVisibility.FAMILY, sync_status=KnowledgeDocument.SYNC_AVAILABLE,
                curation_status=KnowledgeDocument.CURATION_INBOX, knowledge_status=KnowledgeDocument.KNOWLEDGE_PENDING,
                library_tier=KnowledgeDocument.LIBRARY_ARCHIVE, content_created_at=entry.published_at,
                content_modified_at=revision.created_at, hierarchy={'program_entry_id': entry.pk, 'program_revision_id': revision.pk})
            intro = f'{entry.title}\n来源：{entry.url}\n原文版本：{revision.content_hash}\n'
            summary_text = '\n'.join(f"[{p['topic']} / {p['kind']}] {p['text']}（段落 {', '.join(map(str, p['refs']))}）"
                                     for p in revision.summary.get('points', []))
            plain = intro + '\nAI 整理（未经人工复核）\n' + summary_text + '\n\n原文\n' + revision.text
            html = '<h1>' + escape(entry.title) + '</h1><p>' + escape(intro) + '</p><h2>AI 整理（未经人工复核）</h2>'
            html += ''.join('<p>' + escape(line) + '</p>' for line in summary_text.splitlines()) + '<h2>原文</h2>'
            html += ''.join(f'<p id="segment-{i}"><strong>{i}. </strong>{escape(s["text"])}</p>' for i, s in enumerate(revision.segments, 1))
            payload = {'schema': 'program-archive-v1', 'source_url': entry.url, 'origin': revision.origin,
                       'content_hash': revision.content_hash, 'segments': revision.segments, 'summary': revision.summary,
                       'summary_complete': revision.summary_complete}
            raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
            saved = KnowledgeRevision.objects.create(document=document, revision_number=1,
                content_hash=hashlib.sha256(raw).hexdigest(), normalized_hash=hashlib.sha256(plain.encode()).hexdigest(),
                normalized_html=html, plain_text=plain, converter_version='intelligence-program-v1', source_modified_at=revision.updated_at)
            saved.raw_file.save(f'program-{revision.pk}.json', ContentFile(raw), save=True)
            stored = saved.raw_file
            document.current_revision = saved
            document.save(update_fields=['current_revision', 'updated_at'])
            revision.archived_document = document
            revision.save(update_fields=['archived_document', 'updated_at'])
            index_document(document)
            return document
    except Exception:
        if stored:
            stored.delete(save=False)
        raise
