"""Page composition only; AI and acquisition keep their existing explicit POST APIs."""
from django.contrib import messages
from django.http import HttpResponseForbidden
from django.shortcuts import redirect, render
from django.urls import reverse
from ai_analysis.models import AiAnalysisRequest
from .views import _method
from .permissions import get_current_member, get_accessible_dossier_or_404, is_writer
from .company_workspace import workspace_context, research_history
from .next_day_digest import latest_digest
from .valuation_trial import build_valuation_trial
from .filing_review import reviewable_filings


@_method(['GET'])
def follow(request, pk):
    member = get_current_member(request)
    dossier = get_accessible_dossier_or_404(member, pk)
    _, review_items = reviewable_filings(dossier)
    return render(request, 'investment_research/company_materials.html', {
        'dossier': dossier, 'can_write': is_writer(member), 'tracking_page': True,
        'pending_review_count': sum(item['pending'] for item in review_items),
        'needs_revision_count': sum(item['needs_revision'] for item in review_items),
        'digest': latest_digest(dossier), **workspace_context(dossier, {'view': 'changes'})})


@_method(['GET'])
def history(request, pk):
    member = get_current_member(request)
    dossier = get_accessible_dossier_or_404(member, pk)
    drafts = AiAnalysisRequest.objects.filter(member=member, family=member.family,
        module='investment_research', scope__dossier_id=pk).exclude(
        analysis_type__in=['thesis_synthesis', 'company_introduction', 'next_day_digest']).order_by('-created_at')
    # Legacy draft views enforce the request type and dossier ownership again.
    drafts = drafts.filter(analysis_type='document_draft')
    return render(request, 'investment_research/analysis_history.html', {
        'dossier': dossier, 'reports': research_history(dossier), 'drafts': drafts})


@_method(['GET'])
def library_news(request, pk):
    member = get_current_member(request)
    dossier = get_accessible_dossier_or_404(member, pk)
    params = request.GET.copy()
    params.update({'view': 'evidence', 'source_type': 'news'})
    return render(request, 'investment_research/company_materials.html', {
        'dossier': dossier, 'can_write': is_writer(member), 'library_page': True,
        **workspace_context(dossier, params)})


@_method(['GET'])
def valuation(request, pk):
    dossier = get_accessible_dossier_or_404(get_current_member(request), pk)
    report = research_history(dossier).filter(status=AiAnalysisRequest.STATUS_SUCCESS).first()
    return render(request, 'investment_research/valuation.html', {'dossier': dossier,
        'valuation': build_valuation_trial(dossier.security, report.scope if report else {}, request.GET)})


@_method(['POST'])
def observation(request, pk):
    member = get_current_member(request)
    dossier = get_accessible_dossier_or_404(member, pk)
    if not is_writer(member):
        return HttpResponseForbidden('查看者不能修改观察状态。')
    value = request.POST.get('enabled')
    if value not in {'0', '1'}:
        return HttpResponseForbidden('无效的观察状态。')
    dossier.is_watched = value == '1'
    dossier.save(update_fields=['is_watched', 'updated_at'])
    messages.success(request, '已加入观察。' if dossier.is_watched else '已停止观察。次日跟踪的 AI 授权保持原设置。')
    target = request.POST.get('return_to', '')
    allowed = [reverse('investment_research:' + name, args=[pk]) for name in
               ('follow', 'metric_focus', 'review_plan', 'filing_reviews', 'next_day_tracking')]
    return redirect(target if target.split('?')[0] in allowed else reverse('investment_research:follow', args=[pk]))
