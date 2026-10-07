"""Cross-module lookup over existing permission-filtered records; no new index."""
from urllib.parse import urlencode

from django.core.paginator import Paginator
from django.db.models import F, OuterRef, Q, Subquery
from django.urls import reverse
from django.utils.html import strip_tags

from investment_research.models import CompanyMaterialVersion, ResearchQuestion, ResearchQuestionUpdate
from investment_research.permissions import accessible_dossiers
from investment_watch.services import public_versions
from knowledge.models import KnowledgeSearchEntry
from knowledge.permissions import accessible_search_entries
from notes.views import _accessible_notes
from reading.permissions import accessible_books
from reading.annotations import accessible_annotations


GROUPS = [('companies', '公司'), ('research', '研究资料'), ('questions', '本人问题'),
          ('analysis', 'AI 核查'), ('news', '财经资讯'), ('knowledge', '知识资料'),
          ('reading', '在线阅读'), ('annotations', '阅读笔记'), ('notes', '成员笔记')]


def result(title, url, source, nature, when, excerpt='', date_label='内容日期'):
    return dict(title=title, url=url, source=source, nature=nature, when=when,
                excerpt=strip_tags(str(excerpt))[:220], date_label=date_label)


def search_content(member, query, group='all', page=1):
    query = query.strip()[:120]
    if not query:
        return []
    dossiers = accessible_dossiers(member)
    own_dossier = dossiers.filter(security_id=OuterRef('material__security_id')).order_by('pk')
    latest_version = CompanyMaterialVersion.objects.filter(material_id=OuterRef('material_id')).order_by('-number')
    datasets = {
        'companies': (dossiers.filter(Q(security__name__icontains=query) | Q(security__symbol__icontains=query)).order_by('-updated_at', '-pk'),
            lambda d: result(f'{d.security.name} · {d.security.symbol}', reverse('investment_research:prepare', args=[d.pk]),
                             '本人公司研究档案', '公司', d.updated_at, date_label='档案更新')),
        'research': (CompanyMaterialVersion.objects.filter(material__security_id__in=dossiers.values('security_id'),
                      pk=Subquery(latest_version.values('pk')[:1])).filter(Q(material__title__icontains=query) | Q(text__icontains=query))
                      .select_related('material').annotate(dossier_id=Subquery(own_dossier.values('pk')[:1])).defer('raw_gzip', 'data').order_by('-fetched_at', '-pk'),
            lambda v: result(v.material.title, reverse('investment_research:material_read', args=[v.dossier_id, v.pk]),
                             '公司资料 · 第%s版' % v.number, '来源资料', v.fetched_at, v.text, '获取时间')),
        'questions': (ResearchQuestion.objects.filter(dossier__in=dossiers).exclude(status='removed').filter(
                        Q(title__icontains=query) | Q(metrics__icontains=query) | Q(supporting_condition__icontains=query) |
                        Q(reconsidering_condition__icontains=query)).select_related('dossier__security').order_by('-updated_at', '-pk'),
            lambda q: result(q.title, reverse('investment_research:question_detail', args=[q.dossier_id, q.pk]),
                             q.dossier.security.name, '本人确认 · 第%s版' % q.revision, q.updated_at, q.metrics, '问题更新')),
        'analysis': (ResearchQuestionUpdate.objects.filter(question__dossier__in=dossiers,
                      question_revision=F('question__revision')).filter(Q(answer__icontains=query) | Q(change__icontains=query))
                      .select_related('question__dossier__security').order_by('-created_at', '-pk'),
            lambda u: result(u.question.title, reverse('investment_research:question_detail', args=[u.question.dossier_id, u.question_id]),
                             u.question.dossier.security.name, 'AI 核查 · 需本人核对', u.created_at, u.answer, '生成时间')),
        'news': (public_versions(member).exclude(status='withdrawn').filter(Q(title__icontains=query) | Q(summary__icontains=query)).order_by('-published_at', '-pk'),
            lambda v: result(v.title, reverse('investment_watch:news_detail', args=[v.material_id]) + '?' + urlencode({'version': v.pk}),
                             v.material.source.name, '来源资讯', v.published_at, v.summary)),
        'knowledge': (accessible_search_entries(member).exclude(item_kind=KnowledgeSearchEntry.KIND_INVESTMENT_NOTE)
                      .filter(searchable_text__icontains=query).order_by('-content_time', '-pk'),
            lambda e: result(e.title, reverse('knowledge:document_detail', args=[e.document_id]) if e.document_id else reverse('knowledge:artifact_detail', args=[e.artifact_id]),
                             e.source_name, 'AI 专题成果' if e.item_kind == KnowledgeSearchEntry.KIND_ARTIFACT else '归档资料', e.content_time, e.summary or e.body)),
        'reading': (accessible_books(member).filter(Q(title__icontains=query) | Q(author__icontains=query) | Q(description__icontains=query)).order_by('-created_at', '-pk'),
            lambda b: result(b.title, reverse('reading:detail', args=[b.pk]), b.author or '作者未记录', '图书', b.created_at, b.description, '加入书库')),
        'annotations': (accessible_annotations(member)
                      .filter(Q(quote__icontains=query) | Q(note__icontains=query)).select_related('book', 'author').order_by('-updated_at', '-pk'),
            lambda a: result(a.book.title + ' · 阅读笔记', reverse('reading:note', args=[a.pk]), a.author.display_name,
                             '成员批注与原文划线', a.updated_at, a.note or a.quote, '批注更新')),
        'notes': (_accessible_notes(member).filter(Q(title__icontains=query) | Q(content__icontains=query)).order_by('-note_date', '-pk'),
            lambda n: result(n.title, reverse('notes:detail', args=[n.pk]), n.member.display_name, '成员原文', n.note_date, n.content)),
    }
    groups = []
    for key, label in GROUPS:
        if group != 'all' and key != group:
            continue
        queryset, decorate = datasets[key]
        pager = Paginator(queryset, 20 if group != 'all' else 5).get_page(page if group != 'all' else 1)
        groups.append({'key': key, 'label': label, 'page': pager, 'count': pager.paginator.count,
                       'items': [decorate(item) for item in pager]})
    return groups
