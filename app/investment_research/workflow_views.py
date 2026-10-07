"""Daily three-step screens; legacy records remain available as read history."""
import gzip
import json
import uuid
from datetime import timedelta
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import F, Q, Count, Exists, OuterRef, Subquery, CharField
from django.db.models.functions import Cast
from django.db.models.fields.json import KeyTextTransform
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_http_methods
from .permissions import get_current_member, get_accessible_dossier_or_404, accessible_dossiers, is_writer
from .research_ai import available_research_providers, ResearchAiError
from . import preparation, question_ai, question_workflow as workflow
from .models import ResearchQuestion, ResearchQuestionUpdate, ResearchSupplement
from ai_analysis.models import AiAnalysisRequest


def context(request, dossier):
    member = get_current_member(request)
    return {'dossier': dossier, 'can_write': is_writer(member) and dossier.owner_id == member.pk,
            'settings': workflow.settings_for(member), 'providers': available_research_providers(),
            'nonce': str(uuid.uuid4()), 'error': ''}


def provider_for(data, value):
    provider = next((p for p in data['providers'] if str(p.pk) == value), None)
    if not provider:
        raise ResearchAiError('请选择已配置并获准使用的模型。')
    return provider


def status(job):
    return bool(job and job.status in {'pending', 'running'} and job.created_at >= timezone.now() - timedelta(minutes=10))


@login_required
@require_http_methods(['GET'])
def index(request):
    member = get_current_member(request)
    if member is None:
        return HttpResponseForbidden('需要有效的家庭成员身份。')
    dossiers = accessible_dossiers(member).order_by('-updated_at', '-pk')
    selected_filter = request.GET.get('filter', 'all')
    if selected_filter == 'watch':
        dossiers = dossiers.filter(is_watched=True)
    query = request.GET.get('q', '').strip()[:100]
    if query:
        dossiers = dossiers.filter(Q(security__name__icontains=query) | Q(security__symbol__icontains=query))
    # Count and paginate the permission-filtered queryset before decorating rows.
    page = Paginator(dossiers, 20).get_page(request.GET.get('page'))
    jobs = AiAnalysisRequest.objects.filter(family_id=member.family_id, member_id=member.pk,
        module='investment_research').annotate(dossier_key=KeyTextTransform('dossier_id', 'scope')).filter(
            dossier_key=Cast(OuterRef('pk'), CharField()))
    changes = ResearchQuestionUpdate.objects.filter(question__dossier_id=OuterRef('pk'),
        question__status='tracking', question_revision=F('question__revision')).order_by('-created_at', '-pk')
    rows = list(page.object_list.annotate(
        questions_count=Count('research_questions', filter=Q(research_questions__status='tracking')),
        has_questions=Exists(ResearchQuestion.objects.filter(dossier_id=OuterRef('pk')).exclude(status='removed')),
        has_introduction=Exists(jobs.filter(analysis_type=preparation.TYPE, status='success')),
        last_job_status=Subquery(jobs.filter(analysis_type=question_ai.TRACK).order_by('-pk').values('status')[:1]),
        last_change_id=Subquery(changes.values('pk')[:1]),
    ))
    change_map = ResearchQuestionUpdate.objects.in_bulk([d.last_change_id for d in rows if d.last_change_id])
    for dossier in rows:
        dossier.last_change = change_map.get(dossier.last_change_id)
        if dossier.is_watched and dossier.question_workflow and dossier.questions_count:
            dossier.stage_label, dossier.next_name, dossier.next_label = '跟踪中', 'follow', '跟踪问题'
        elif dossier.has_questions:
            dossier.stage_label, dossier.next_name, dossier.next_label = '问题待确认', 'questions', '提出问题'
        elif dossier.has_introduction:
            dossier.stage_label, dossier.next_name, dossier.next_label = '已了解公司', 'questions', '提出问题'
        else:
            dossier.stage_label, dossier.next_name, dossier.next_label = '初识中', 'prepare', '了解公司'
        if dossier.research_paused:
            dossier.stage_label = '暂时结束'
        dossier.next_url = reverse('investment_research:' + dossier.next_name, args=[dossier.pk])
    page.object_list = rows
    return render(request, 'investment_research/workflow_index.html', {'dossiers': page, 'page': page, 'query': query,
        'selected_filter': selected_filter, 'can_write': is_writer(member)})


@login_required
@require_http_methods(['GET', 'POST'])
def introduction(request, pk):
    member = get_current_member(request)
    dossier = get_accessible_dossier_or_404(member, pk)
    data = context(request, dossier)
    reports = preparation.history(dossier).select_related('result', 'provider')
    selected = request.GET.get('report')
    job = get_object_or_404(reports, pk=selected) if selected and selected.isdigit() else reports.first()
    if request.method == 'POST':
        try:
            preparation.authorize(member, dossier)
            if request.POST.get('action') == 'pause':
                dossier.research_paused = True
                dossier.save(update_fields=['research_paused', 'updated_at'])
                messages.success(request, '已暂时结束研究，报告和问题仍保留。')
                return redirect('investment_research:index')
            job = preparation.enqueue(member, dossier, provider_for(data, request.POST.get('provider')),
                request.POST.get('consent') == 'yes', request.POST.get('nonce'),
                include_web=request.POST.get('include_web') == 'yes', simple=True,
                requirements=request.POST.get('requirements', ''))
            messages.success(request, '初识报告正在后台生成。完成后可阅读，再决定是否提出问题。')
            return redirect(f'{request.path}?report={job.pk}')
        except ResearchAiError as exc:
            data['error'] = str(exc)
    success = job if job and job.status == 'success' else reports.filter(status='success').first()
    data.update(job=job, report=success, result=success.result.result_json if success else None,
        evidence=success.sanitized_input.get('evidence', []) if success else [], active=status(job), reports=reports[:20],
        affected=list(workflow.visible_questions(dossier).filter(introduction_id__isnull=False).exclude(introduction=success)) if success else [],
        cutoff=max((e.get('fetched_at', '') for e in success.sanitized_input.get('evidence', [])), default='未标注') if success else '',
        requirements=request.POST.get('requirements', ''))
    if success:
        try:
            parsed_cutoff = parse_datetime(data['cutoff'])
        except (ValueError, TypeError):
            parsed_cutoff = None
        if parsed_cutoff:
            data['cutoff'] = timezone.localtime(parsed_cutoff).strftime('%Y-%m-%d %H:%M') if timezone.is_aware(parsed_cutoff) else parsed_cutoff.strftime('%Y-%m-%d %H:%M')
        extra = {key: success.result.result_json[key] for key in ('questions', 'hypotheses') if success.result.result_json.get(key)}
        data['legacy_extra'] = json.dumps(extra, ensure_ascii=False, indent=2) if extra else ''
    return render(request, 'investment_research/workflow_introduction.html', data)


@login_required
@require_http_methods(['GET', 'POST'])
def questions(request, pk):
    member = get_current_member(request)
    dossier = get_accessible_dossier_or_404(member, pk)
    data = context(request, dossier)
    reports = question_ai.history(dossier, question_ai.SUGGEST).select_related('result', 'provider')
    job = reports.first()
    if request.method == 'POST':
        try:
            action = request.POST.get('action')
            if action == 'suggest':
                job = question_ai.enqueue(member, dossier, provider_for(data, request.POST.get('provider')),
                    kind=question_ai.SUGGEST, consent=request.POST.get('consent') == 'yes', nonce=request.POST.get('nonce'),
                    requirements=request.POST.get('requirements', ''))
                messages.success(request, '问题建议正在后台生成。请逐项选择、修改后确认。')
            elif action == 'start':
                workflow.start_tracking(member, dossier, request.POST.get('list_revision'))
                messages.success(request, '已开始跟踪。自动 AI 默认关闭，可在研究设置中开启。')
                return redirect('investment_research:follow', pk=pk)
            elif action == 'save_suggestions':
                suggestion = get_object_or_404(reports, pk=request.POST.get('report'), status='success')
                if suggestion.scope.get('superseded') or suggestion.scope['question_list_revision'] != dossier.question_list_revision:
                    raise ResearchAiError('清单已更新，请重新核对建议或手动添加。')
                selected = request.POST.getlist('selected')
                if not selected:
                    raise ResearchAiError('请至少选择一个建议，也可直接手动添加。')
                with transaction.atomic():
                    revision = request.POST.get('list_revision')
                    for i, item in enumerate(suggestion.result.result_json['questions']):
                        if str(i) not in selected:
                            continue
                        values = {field: request.POST.get(f'{field}_{i}', item[field]) for field in workflow.QUESTION_FIELDS}
                        workflow.save_question(member, dossier, values, expected_list_revision=revision,
                            introduction=workflow.latest_introduction(dossier))
                        dossier.refresh_from_db()
                        revision = dossier.question_list_revision
                messages.success(request, '所选问题已确认；未选择的建议没有加入清单。')
            elif action == 'add':
                workflow.save_question(member, dossier, request.POST, expected_list_revision=request.POST.get('list_revision'),
                    introduction=workflow.latest_introduction(dossier))
                messages.success(request, '问题已加入清单，可继续修改或开始跟踪。')
            else:
                raise ResearchAiError('操作无效。')
            return redirect(request.path)
        except ResearchAiError as exc:
            data['error'] = str(exc)
    dossier.refresh_from_db()
    state = request.GET.get('status', 'tracking')
    rows = workflow.visible_questions(dossier)
    if state in {'tracking', 'resolved', 'paused'}:
        rows = rows.filter(status=state)
    data.update(questions=workflow.attach_updates(list(rows)), state=state, job=job, active=status(job),
        suggestion=job.result.result_json if job and job.status == 'success' else None,
        suggestion_stale=job and (job.scope.get('superseded') or job.scope['question_list_revision'] != dossier.question_list_revision),
        requirements=request.POST.get('requirements', ''))
    return render(request, 'investment_research/workflow_questions.html', data)


@login_required
@require_http_methods(['GET', 'POST'])
def question_detail(request, pk, question_pk):
    member = get_current_member(request)
    dossier = get_accessible_dossier_or_404(member, pk)
    question = get_object_or_404(ResearchQuestion, dossier=dossier, pk=question_pk)
    data = context(request, dossier)
    if request.method == 'POST':
        try:
            if request.POST.get('action') == 'status':
                workflow.set_question_status(member, dossier, question.pk, request.POST.get('status'), request.POST.get('revision'),
                                             request.POST.get('list_revision', ''))
            else:
                workflow.save_question(member, dossier, request.POST, question_id=question.pk,
                    expected_revision=request.POST.get('revision'), expected_list_revision=request.POST.get('list_revision'))
            messages.success(request, '问题已更新，原版本及核查记录保留。')
            return redirect(request.path)
        except ResearchAiError as exc:
            data['error'] = str(exc)
    from .question_sections import history_sections
    revisions = list(question.revisions.select_related('created_by'))
    revision_content = {revision.number: revision.content for revision in revisions}
    field_labels = {'title': '问题', 'supporting_condition': '更相信的证据',
                    'reconsidering_condition': '重新考虑的证据', 'metrics': '指标', 'source_notes': '来源'}
    for revision in revisions:
        previous = revision_content.get(revision.number - 1)
        revision.changes = [{'label': label, 'before': previous.get(key, ''), 'after': revision.content.get(key, '')}
                            for key, label in field_labels.items()
                            if previous is not None and previous.get(key, '') != revision.content.get(key, '')]
    updates = Paginator(question.updates.select_related('analysis__result'), 20).get_page(request.GET.get('history_page'))
    for update in updates:
        update.source_sections = history_sections(question, update, revision_content)
    data.update(question=workflow.attach_updates([question])[0], revisions=revisions,
        updates=updates, actions=question.actions.select_related('created_by'))
    if request.method == 'POST' and data['error'] and request.POST.get('action') != 'status':
        data['question_form'] = request.POST
    return render(request, 'investment_research/workflow_question_detail.html', data)


@login_required
@require_http_methods(['GET', 'POST'])
def tracking(request, pk):
    member = get_current_member(request)
    dossier = get_accessible_dossier_or_404(member, pk)
    data = context(request, dossier)
    job = question_ai.history(dossier).select_related('result', 'provider').first()
    if request.method == 'POST':
        try:
            question_ai.enqueue(member, dossier, provider_for(data, request.POST.get('provider')),
                consent=request.POST.get('consent') == 'yes', nonce=request.POST.get('nonce'),
                requirements=request.POST.get('requirements', ''))
            messages.success(request, '正在后台逐项核查；资料不足会保留缺口。')
            return redirect(request.path)
        except ResearchAiError as exc:
            data['error'] = str(exc)
    rows = workflow.attach_updates(list(workflow.active_questions(dossier)))
    rows.sort(key=lambda q: (0 if q.legacy_kind == 'pillar' else 1, q.position, q.pk))
    changes = [q for q in rows if q.latest_update and (q.legacy_answer or q.latest_update.direction in {'strengthened', 'weakened'} or q.latest_update.gap)]
    quiet = [q for q in rows if q not in changes]
    from .models import ResearchSourceState
    from investment_watch.models import BodyAttempt
    acquisition = dossier.acquisition_jobs.order_by('-pk').first()
    from .company_workspace import research_history
    from .valuation_trial import build_valuation_trial
    report = research_history(dossier).filter(status='success').first()
    data['valuation'] = build_valuation_trial(dossier.security, report.scope if report else {}, request.GET)
    data['original_judgment'] = dossier.current_revision
    data.update(questions=changes, quiet_questions=quiet, job=job, active=status(job),
        consent=dossier.auto_digest_consent if hasattr(dossier, 'auto_digest_consent') else None,
        source_errors=ResearchSourceState.objects.filter(security=dossier.security).exclude(last_error__in=['', None]),
        acquisition=acquisition, acquisition_errors=[i for i in acquisition.items if i.get('status') == 'failed'] if acquisition else [],
        news_errors=BodyAttempt.objects.filter(candidate__dossier=dossier, status='failed').select_related('material_version').order_by('-pk')[:5],
        reports=question_ai.history(dossier).select_related('result', 'provider')[:20])
    return render(request, 'investment_research/workflow_tracking.html', data)


@login_required
@require_http_methods(['GET', 'POST'])
def settings(request, pk):
    member = get_current_member(request)
    dossier = get_accessible_dossier_or_404(member, pk)
    data = context(request, dossier)
    if request.method == 'POST':
        try:
            with transaction.atomic():
                automatic = request.POST.get('automatic') == 'yes'
                provider = next((p for p in data['providers'] if str(p.pk) == request.POST.get('provider')), None)
                if automatic and not provider:
                    raise ResearchAiError('请选择已配置并获准使用的模型。')
                row = workflow.save_settings(member, dossier, request.POST, provider, request.POST.get('revision'))
                workflow.set_tracking_consent(member, dossier, automatic, provider, row.daily_budget_usd)
            messages.success(request, '研究设置已保存，仅影响未来生成。自动授权只用于本公司问题跟踪。')
            return redirect(request.path)
        except ResearchAiError as exc:
            data['error'] = str(exc)
    data.update(consent=dossier.auto_digest_consent if hasattr(dossier, 'auto_digest_consent') else None,
        posted=request.POST if request.method == 'POST' else None)
    data['automatic_enabled'] = bool(data['consent'] and not data['consent'].revoked_at)
    if request.method == 'POST' and data['error']:
        data['settings'] = request.POST.dict() | {'provider_id': int(request.POST.get('provider'))
            if request.POST.get('provider', '').isdigit() else None}
        data['automatic_enabled'] = request.POST.get('automatic') == 'yes'
    return render(request, 'investment_research/workflow_settings.html', data)


@login_required
@require_http_methods(['GET', 'POST'])
def supplement(request, pk):
    from .manual_materials import add
    member = get_current_member(request)
    dossier = get_accessible_dossier_or_404(member, pk)
    data = context(request, dossier)
    if request.method == 'POST':
        try:
            row, created = add(member, dossier, request.POST, request.FILES.get('file'))
            messages.success(request, '资料已补充。' if created else '这份原件已有记录，已打开原资料。')
            return redirect('investment_research:supplement_read', pk=pk, supplement_pk=row.pk)
        except ResearchAiError as exc:
            data['error'] = str(exc)
    return render(request, 'investment_research/workflow_supplement.html', data)


@login_required
@require_http_methods(['GET'])
def supplement_read(request, pk, supplement_pk):
    dossier = get_accessible_dossier_or_404(get_current_member(request), pk)
    row = get_object_or_404(ResearchSupplement, dossier=dossier, pk=supplement_pk)
    if request.GET.get('download') == '1':
        response = HttpResponse(gzip.decompress(bytes(row.raw_gzip)), content_type='application/octet-stream')
        response['Content-Disposition'] = f'attachment; filename="supplement-{row.pk}{Path_suffix(row.original_name)}"'
        response['X-Content-Type-Options'] = 'nosniff'
        response['Content-Security-Policy'] = 'sandbox'
        return response
    return render(request, 'investment_research/workflow_source.html', {'dossier': dossier, 'source': row,
        'title': row.title, 'text': row.text, 'note': row.extraction_note, 'date': row.period or row.published_at,
        'url': row.source_url, 'fetched_at': row.created_at, 'download': '?download=1'})


def Path_suffix(name):
    from pathlib import Path
    suffix = Path(name).suffix.lower()
    return suffix if suffix in {'.pdf', '.docx', '.xlsx', '.pptx', '.html', '.txt', '.md'} else '.bin'


@login_required
@require_http_methods(['GET'])
def news_source(request, pk, version_pk):
    from investment_watch.models import MaterialVersion, BodySnapshot
    dossier = get_accessible_dossier_or_404(get_current_member(request), pk)
    row = get_object_or_404(MaterialVersion.objects.filter(researchcandidate__dossier=dossier,
        material__source__family=dossier.family).distinct(), pk=version_pk)
    body = BodySnapshot.objects.filter(material_version=row).first()
    return render(request, 'investment_research/workflow_source.html', {'dossier': dossier, 'title': row.title,
        'text': body.text if body else row.summary, 'note': '已存新闻正文' if body else '仅标题与摘要线索，尚无已存正文。',
        'date': row.published_at, 'fetched_at': body.fetched_at if body else row.found_at, 'url': row.url})


@login_required
@require_http_methods(['GET', 'POST'])
def legacy_redirect(request, pk):
    member = get_current_member(request)
    get_accessible_dossier_or_404(member, pk)
    if request.method == 'POST' and not is_writer(member):
        return HttpResponseForbidden('当前成员不可修改问题。')
    return redirect('investment_research:questions', pk=pk)
