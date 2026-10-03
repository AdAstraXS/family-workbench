"""Three daily steps, with sources/settings in the secondary navigation."""
from urllib.parse import urlencode
from django.urls import reverse
from .permissions import accessible_dossiers, get_current_member


def url(name, pk=None, **query):
    path = reverse('investment_research:' + name, args=[pk] if pk else [])
    return path + ('?' + urlencode(query) if query else '')


def company_state(dossier):
    stage = 'follow' if dossier.question_workflow and dossier.is_watched else 'questions' if dossier.research_questions.exists() else 'prepare'
    return {'stage': stage, 'label': {'follow': '跟踪问题', 'questions': '提出问题', 'prepare': '了解公司'}[stage],
            'watching': dossier.question_workflow and dossier.is_watched, 'paused': dossier.research_paused}


def navigation(context):
    request = context['request']
    name = request.resolver_match.url_name
    dossier = context.get('dossier')
    result = {'crumbs': [{'label': '投研 / 公司列表', 'url': url('index')}],
              'title': '公司列表', 'area': '', 'tabs': [], 'stages': [], 'aux': [], 'companies': []}
    if not dossier:
        title = '了解一家公司' if name in {'material_start', 'explore', 'create'} else '公司官方 IR' if name == 'ir_catalogue' else '公司列表'
        result['title'] = title
        if name == 'index':
            result['crumbs'][0].pop('url')
        else:
            result['crumbs'].append({'label': title})
        return result
    stages = [('prepare', '了解公司', '阅读生意、竞争、财务、机会与风险', 'prepare'),
              ('questions', '提出问题', '确认问题、证据条件与指标来源', 'questions'),
              ('follow', '跟踪问题', '查看新增证据及答案变化', 'follow')]
    areas = {'prepare': {'prepare', 'preparation_source'}, 'questions': {'questions', 'question_detail', 'company_research'},
             'follow': {'follow', 'filing_review', 'filing_reviews', 'next_day_tracking'},
             'settings': {'research_settings', 'prompt_settings'}}
    area = next((key for key, routes in areas.items() if name in routes), 'library')
    if name == 'materials' and request.GET.get('context') == 'prepare':
        area = 'prepare'
    result['area'], result['state'] = area, company_state(dossier)
    for i, (key, label, hint, route) in enumerate(stages, 1):
        result['stages'].append({'key': key, 'label': label, 'hint': hint, 'number': i,
                                 'url': url(route, dossier.pk), 'active': area == key})
    result['aux'] = [{'label': '资料', 'url': url('materials', dossier.pk), 'active': area == 'library'},
                     {'label': '研究设置', 'url': url('research_settings', dossier.pk), 'active': area == 'settings'}]
    title = {'prepare': '了解公司', 'questions': '提出问题', 'follow': '跟踪问题', 'library': '资料', 'settings': '研究设置'}[area]
    detail = {'material_read': '阅读资料', 'document_detail': '阅读官方原文', 'document_metrics': '年报指标明细',
              'financials': 'SEC 财务整理报表', 'futu_financials': '富途财务报表', 'valuation': '行情与估值',
              'question_detail': '问题详情', 'supplement': '人工补充', 'supplement_read': '阅读补充资料',
              'question_news_source': '阅读新闻资料', 'research_history': '旧流程历史记录',
              'thesis_analysis_detail': '历史研究报告', 'history': '旧判断历史', 'detail': '旧流程记录',
              'preparation_source': '初识报告原文快照'}.get(name)
    if name == 'materials' and request.GET.get('tab') == 'acquisition':
        detail = '获取与更新资料'
    result['crumbs'] += [{'label': dossier.security.name, 'url': url('prepare', dossier.pk)},
                         {'label': detail or title}]
    result['title'] = detail or title
    switch_route = {'prepare': 'prepare', 'preparation_source': 'prepare', 'questions': 'questions',
                    'question_detail': 'questions', 'follow': 'follow', 'research_settings': 'research_settings',
                    'prompt_settings': 'research_settings'}.get(name, 'materials')
    switch_query = {k:request.GET[k] for k in ['category', 'context', 'tab'] if k in request.GET} if switch_route == 'materials' else {}
    result['companies'] = [{'label': f'{d.security.name} · {d.security.symbol}',
                           'url': url(switch_route, d.pk, **switch_query), 'selected': d.pk == dossier.pk}
                          for d in accessible_dossiers(get_current_member(request)).order_by('security__symbol')]
    return result
