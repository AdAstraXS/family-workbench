"""Member-scoped, read-only task and freshness summaries."""
from datetime import timedelta

from django.db.models import Max, OuterRef, Q, Subquery
from django.urls import reverse
from django.utils import timezone

from intelligence.program_models import ProgramEntry, ProgramSubscription
from intelligence.program_progress import program_progress
from investment_research.permissions import accessible_dossiers
from investment_research.models import CompanyAcquisitionJob
from investment_watch.models import NewsSource
from macro.models import MacroAlert, MacroImportRun, MacroObservation, MacroSourceMapping
from knowledge.models import KnowledgeSource, KnowledgeJob
from reading.permissions import accessible_books


def task_health(member):
    now = timezone.now()
    tasks, sources = [], []
    entries = ProgramEntry.objects.filter(subscription__family=member.family).filter(
        Q(private_owner__isnull=True) | Q(private_owner=member)
    ).filter(state__in=['failed', 'uncertain', 'waiting_config']).select_related('current_revision', 'subscription')
    for entry in entries.order_by('-updated_at')[:50]:
        progress = program_progress(entry)
        tasks.append({'title': entry.title, 'module': '精选订阅', 'stage': progress['stage'],
                      'completed': '；'.join(progress['completed']) or '尚无已保存步骤',
                      'next_step': progress['next_step'], 'time': entry.updated_at,
                      'url': reverse('intelligence:program_detail', args=[entry.pk])})
    jobs = CompanyAcquisitionJob.objects.filter(dossier__in=accessible_dossiers(member)).order_by('-created_at')
    # Show only the latest acquisition per dossier; a later success resolves an old failure.
    latest = jobs.filter(dossier_id=OuterRef('dossier_id')).values('pk')[:1]
    for job in jobs.filter(pk=Subquery(latest), status__in=['failed', 'partial', 'interrupted']).select_related('dossier__security')[:30]:
        tasks.append({'title': job.dossier.security.name, 'module': '投研', 'stage': '获取公司资料',
                      'completed': '已取得的资料保留在资料表', 'next_step': '打开资料页查看各来源结果并补充失败来源。',
                      'time': job.finished_at or job.updated_at,
                      'url': reverse('investment_research:materials', args=[job.dossier_id])})
    for book in accessible_books(member).filter(file__status='failed').order_by('-file__updated_at')[:20]:
        tasks.append({'title': book.title, 'module': '在线阅读', 'stage': '解析图书',
                      'completed': '原始文件已保存', 'next_step': '打开图书详情查看错误；书籍所有者可按原流程重新解析。',
                      'time': book.file.updated_at, 'url': reverse('reading:detail', args=[book.pk])})
    last_sync = KnowledgeJob.objects.filter(source_id=OuterRef('pk'), job_type='sync_source').order_by('-created_at')
    for source in KnowledgeSource.objects.filter(family=member.family, owner=member, is_enabled=True).annotate(
            checked=Subquery(last_sync.values('started_at')[:1]),
            success=Subquery(last_sync.filter(status='success').values('finished_at')[:1])):
        sources.append({'name': source.name, 'module': '本人知识来源', 'period': None, 'success': source.success,
                        'checked': source.checked, 'status': '同步需处理' if source.last_error else '按资料详情核对',
                        'attention': bool(source.last_error), 'url': reverse('knowledge:source_detail', args=[source.pk])})
        if source.last_error:
            tasks.append({'title': source.name, 'module': '知识中心', 'stage': '同步来源',
                          'completed': '此前保存的原文与版本保留', 'next_step': '打开来源详情核对授权或同步错误，再按原流程续跑。',
                          'time': source.checked, 'url': reverse('knowledge:source_detail', args=[source.pk])})
    for source in NewsSource.objects.filter(family=member.family, enabled=True).annotate(
            content_date=Max('materials__current_version__published_at')).order_by('name'):
        overdue = not source.last_checked_at or now - source.last_checked_at > timedelta(minutes=max(60, source.interval_minutes * 2))
        sources.append({'name': source.name, 'module': '财经资讯', 'period': source.content_date,
                        'success': source.last_success_at, 'checked': source.last_checked_at,
                        'status': '获取失败' if source.last_error else '检查逾期' if overdue else '已检查',
                        'attention': bool(source.last_error) or overdue, 'url': reverse('investment_watch:runs')})
    subscriptions = ProgramSubscription.objects.filter(family=member.family, enabled=True, removed_at__isnull=True).filter(
        ~Q(kind='upload') | Q(code=f'upload_{member.pk}'))
    # Filter entries before computing dates: a private upload must not affect another member's summary.
    visible_entries = ProgramEntry.objects.filter(subscription_id=OuterRef('pk')).filter(
        Q(private_owner__isnull=True) | Q(private_owner=member)).order_by('-published_at')
    for source in subscriptions.annotate(content_date=Subquery(visible_entries.values('published_at')[:1])).order_by('code'):
        if source.kind == 'upload':
            continue
        sources.append({'name': source.custom_name or source.code, 'module': '精选订阅', 'period': source.content_date,
                        'success': source.last_success_at, 'checked': source.last_checked_at,
                        'status': '获取失败' if source.last_error else '已检查' if source.last_checked_at else '尚未检查',
                        'attention': bool(source.last_error), 'url': reverse('intelligence:program_list')})
    latest_observation = MacroObservation.objects.filter(mapping_id=OuterRef('pk'), value__isnull=False).order_by('-period_date')
    latest_run = MacroImportRun.objects.filter(group=OuterRef('group')).order_by('-started_at')
    late_indicators = set(MacroAlert.objects.filter(resolved_at__isnull=True, key__startswith='late:').values_list('key', flat=True))
    for mapping in MacroSourceMapping.objects.filter(indicator__is_active=True).select_related('indicator').annotate(
        period=Subquery(latest_observation.values('period_date')[:1]),
        checked=Subquery(latest_run.values('started_at')[:1]),
        run_status=Subquery(latest_run.values('status')[:1]),
        success=Subquery(latest_run.filter(status='success').values('finished_at')[:1]),
    ).order_by('indicator__country', 'indicator__name'):
        overdue = f'late:{mapping.indicator.country}:{mapping.indicator.code}' in late_indicators
        sources.append({'name': str(mapping.indicator), 'module': '宏观数据', 'period': mapping.period,
                        'success': mapping.success, 'checked': mapping.checked,
                        'status': '应更新统计期尚未取得' if overdue else '获取失败' if mapping.run_status == 'failed' else '尚无数据' if not mapping.period else '按统计期核对',
                        'attention': overdue or mapping.run_status == 'failed' or not mapping.period,
                        'url': reverse('macro:indicator', args=[mapping.indicator.country, mapping.indicator.code])})
    sources.sort(key=lambda row: (not row['attention'], row['module'], row['name']))
    return {'tasks': tasks, 'sources': sources, 'health_checked_at': now}
