"""One navigation contract for every private company research page."""
from urllib.parse import urlencode

from django.urls import reverse

from .permissions import accessible_dossiers, get_current_member


def url(name, pk=None, **query):
    path = reverse('investment_research:' + name, args=[pk] if pk else [])
    return path + ('?' + urlencode(query) if query else '')


def company_state(dossier):
    prep = dossier.preparations.last()
    decision = prep.decision if prep else ''
    stage = 'judgment' if dossier.current_revision_id else 'research' if decision == 'research' else 'initial'
    return {'stage': stage, 'label': {'judgment': '已有判断', 'research': '深入研究', 'initial': '初识中'}[stage],
            'watching': dossier.is_watched, 'paused': decision == 'pause'}


def navigation(context):
    request = context['request']
    name = request.resolver_match.url_name
    dossier = context.get('dossier')
    root = {'label': '投研 / 公司列表', 'url': url('index')}
    result = {'crumbs': [root], 'title': '公司列表', 'area': '', 'tabs': [], 'stages': [], 'aux': []}
    if not dossier:
        title = {'material_start': '了解一家公司', 'explore': '了解一家公司', 'create': '记录已有判断', 'ir_catalogue': '公司官方 IR'}.get(name, '公司列表')
        result['title'] = title
        if name != 'index':
            result['crumbs'].append({'label': title})
        else:
            result['crumbs'][0].pop('url')
        return result
    pk = dossier.pk
    stage_defs = [('research', '公司研究', '关键问题 · 证据 · 我的判断', 'company_research'),
                  ('follow', '持续跟踪', '观察变化 · 定期复核', 'follow')]
    area = next((area for area, names in {
        'prepare': {'prepare', 'prompt_settings', 'preparation_source'},
        'research': {'company_research', 'thesis_analysis'},
        'history': {'research_history', 'thesis_analysis_detail', 'draft_detail'},
        'judgment': {'detail', 'edit', 'first_thesis', 'history'},
        'follow': {'follow', 'metric_focus', 'review_plan', 'filing_reviews', 'filing_review', 'next_day_tracking'},
        'library': {'materials', 'material_read', 'documents', 'document_detail', 'library_news', 'financials', 'futu_financials', 'document_metrics', 'valuation'},
    }.items() if name in names), 'library')
    if name == 'materials' and request.GET.get('context') == 'prepare':
        area = 'prepare'
    result['area'] = area
    result['state'] = company_state(dossier)
    for i, (key, label, hint, route) in enumerate(stage_defs, 1):
        result['stages'].append({'key': key, 'label': label, 'hint': hint, 'number': i, 'url': url(route, pk), 'active': area == key or key == 'research' and area in {'prepare', 'judgment'}})
    result['aux'] = [{'label': label, 'url': url(route, pk), 'active': area == key}
                     for key, label, route in [('library', '资料', 'materials'), ('history', '历史', 'research_history')]]
    tab = 'questions' if request.POST.get('action') == 'confirm' else request.GET.get('tab', '')
    definitions = {
        'prepare': [('准备资料', url('materials', pk, context='prepare'), name == 'materials'),
                    ('公司初识', url('prepare', pk), name == 'prepare' and tab != 'questions'),
                    ('问题与候选假设', url('prepare', pk, tab='questions'), name == 'prepare' and tab == 'questions'),
                    ('提示词设置', url('prompt_settings', pk), name == 'prompt_settings')],
        'research': [],
        'judgment': [('当前判断', url('detail', pk), name != 'history'), ('判断历史', url('history', pk), name == 'history')],
        'follow': [('公司动态', url('follow', pk), name in {'follow', 'next_day_tracking'}),
                   ('跟踪计划', url('metric_focus', pk), name in {'metric_focus', 'review_plan'}),
                   ('复核记录', url('filing_reviews', pk), name in {'filing_reviews', 'filing_review'})],
        'history': [],
        'library': [('SEC', url('materials', pk, category='sec'), name in {'financials', 'document_metrics'} or context.get('category', 'sec') == 'sec' and name == 'materials'),
                    ('富途', url('materials', pk, category='futu'), name == 'futu_financials' or context.get('category') == 'futu'),
                    ('公司 IR', url('materials', pk, category='ir'), name in {'documents', 'document_detail'} or context.get('category') == 'ir'),
                    ('行情与估值', url('valuation', pk), name == 'valuation'),
                    ('其他', url('materials', pk, category='other'), context.get('category') == 'other')],
        'financial': [('财务概览', url('financials', pk), name in {'financials', 'document_metrics'}),
                      ('财务报表', url('futu_financials', pk), name == 'futu_financials'),
                      ('行情与估值', url('valuation', pk), name == 'valuation')],
    }
    if name == 'material_read' and context.get('version'):
        material = context['version'].material
        source_index = (0 if material.kind in {'facts', 'sec_document', 'sec'} else
                        2 if material.kind == 'ir' else
                        1 if material.kind in {'profile', 'financials', 'research'} else 4)
        definitions['library'] = [(label, href, i == source_index)
                                  for i, (label, href, _) in enumerate(definitions['library'])]
    result['tabs'] = [{'label': label, 'url': href, 'active': active} for label, href, active in definitions[area]]
    if name in {'company_research', 'follow', 'detail', 'first_thesis', 'edit'} and area != 'library':
        result['tabs'] = []
    if name == 'prepare' and request.GET.get('report', '').isdigit():
        for item in result['tabs'][1:]:
            item['url'] += ('&' if '?' in item['url'] else '?') + urlencode({'report': request.GET['report']})
    area_label = dict((key, label) for key, label, *_ in stage_defs) | {'prepare': '研究问题', 'judgment': '我的判断', 'library': '资料', 'financial': '财务与行情', 'history': '历史'}
    active = next((item for item in result['tabs'] if item['active']), None)
    result['crumbs'].append({'label': dossier.security.name, 'url': url('company_research', pk)})
    result['crumbs'].append({'label': area_label[area], 'url': next((s['url'] for s in result['stages'] + result['aux'] if s['active']), '')})
    if active:
        result['crumbs'].append({'label': active['label'], 'url': active['url']})
    detail = {'thesis_analysis': '更新公司研究', 'thesis_analysis_detail': '历史研究报告', 'draft_detail': '历史 AI 草稿',
              'edit': '修订个人判断', 'first_thesis': '保存第一版判断', 'review_plan': '财报复核计划',
              'filing_review': '记录财报复核', 'next_day_tracking': '次日跟踪设置' if tab == 'settings' else '次日跟踪摘要',
              'material_read': '阅读资料', 'document_detail': '阅读官方原文', 'document_metrics': '年报指标明细',
              'documents': '官方文件目录', 'preparation_source': '研究原文快照'}.get(name)
    if name == 'materials' and tab == 'acquisition':
        detail = '获取与更新资料'
    if detail:
        result['crumbs'].append({'label': detail})
    result['title'] = detail or (active['label'] if active else area_label[area])
    result['crumbs'][-1].pop('url', None)
    # A document/report ID belongs to one company. Switching returns to its parent tab.
    switch_route = {'thesis_analysis_detail': 'research_history', 'draft_detail': 'research_history',
                    'filing_review': 'filing_reviews', 'document_detail': 'documents',
                    'document_metrics': 'financials', 'material_read': 'materials', 'preparation_source': 'prepare'}.get(name, name)
    switch_query = {key: request.GET[key] for key in ('tab', 'context', 'category', 'layer') if key in request.GET}
    if name == 'material_read':
        switch_query['category'] = ['sec', 'futu', 'ir', 'market', 'other'][source_index]
    result['companies'] = [{'label': f'{item.security.name} · {item.security.symbol}',
                            'url': url(switch_route, item.pk, **switch_query), 'selected': item.pk == pk}
                           for item in accessible_dossiers(get_current_member(request)).order_by('security__symbol')]
    return result
