"""One source inventory; originals and genuine derived reports share a table."""
from django.core.paginator import Paginator
from django.urls import reverse
from django.utils.dateparse import parse_date
from .official_ir import documents_for_security

KINDS = {'profile': ('futu', '公司资料'), 'financials': ('futu', '财务数据'), 'research': ('futu', '研报'),
         'facts': ('sec', '结构化财务数据'), 'sec_document': ('sec', '官方披露原件'),
         'sec': ('sec', '披露目录'), 'sec_index': ('sec', '披露目录'), 'ir': ('ir', '官方 IR 原件')}
SOURCES = [('all', '全部来源'), ('sec', 'SEC'), ('futu', '富途'), ('ir', '公司 IR'), ('manual', '人工补充'), ('other', '其他')]


def rows_for(dossier, materials, overview):
    rows, seen = [], set()
    for item in materials:
        version = item.latest
        category, kind = KINDS.get(item.kind, ('other', '历史存档'))
        row = {'title': item.title, 'source': dict(SOURCES).get(category, '其他'), 'category': category,
            'kind': kind, 'period': version.report_date if version else '', 'published': '',
            'fetched': version.fetched_at if version else None, 'layer': 'original', 'layer_label': '原始',
            'summary': '已停用来源，保留历史原件。' if item.retired else
                       'SEC 原始结构化数据；阅读页可查看来源单位和各指标日期。' if item.kind == 'facts' else
                       '富途返回的原始财务数据，页面按报表呈现。' if item.kind == 'financials' else
                       (version.text[:180].replace('\n', ' ') if version and version.text else '来源数据及版本在详情中查看。'),
            'status': '最近获取失败：' + item.last_error if item.last_error else '可读' if version else '尚未取得',
            'url': reverse('investment_research:material_read', args=[dossier.pk, version.pk]) if version else ''}
        if version:
            seen.add(version.source_url)
            row['published'] = version.data.get('published_at') or version.data.get('filing_date') or version.data.get('filed') or '' if isinstance(version.data, dict) else ''
        rows.append(row)
    if any(m.kind == 'facts' and m.latest for m in materials):
        facts = next(m for m in materials if m.kind == 'facts' and m.latest)
        rows.append({'title': 'SEC 年度／季度财务整理报表', 'source': 'SEC', 'category': 'sec', 'kind': '财务整理报表',
            'period': max((r.get('period', '') for r in overview.get('annual', [])), default='') if isinstance(overview, dict) else '', 'published': '',
            'fetched': facts.latest.fetched_at, 'layer': 'prepared', 'layer_label': '整理',
            'summary': '由已存 SEC 数据整理；详情保留期间、单位、直接披露／计算说明和原始数据链接。',
            'status': '可读', 'url': reverse('investment_research:material_read', args=[dossier.pk, facts.latest.pk]) + '?view=prepared'})
    for doc in documents_for_security(dossier.security).prefetch_related('content_versions'):
        version = next(iter(doc.content_versions.all()), None)
        if version and version.source_url in seen:
            continue
        category = 'sec' if doc.source == 'sec' else 'ir'
        rows.append({'title': doc.title, 'source': 'SEC' if category == 'sec' else '公司 IR', 'category': category,
            'kind': doc.get_document_type_display(), 'period': str(doc.period_end or ''),
            'published': str(doc.published_at or ''), 'fetched': version.fetched_at if version else None,
            'layer': 'original', 'layer_label': '原始', 'summary': (version.content_text[:180].replace('\n', ' ') if version else '已取得目录；尚未取得原件正文。'),
            'status': '可读' if version and version.content_text else '原件已存，待核对' if version else '仅目录',
            'url': reverse('investment_research:document_detail', args=[dossier.pk, doc.pk]) + (f'?version={version.pk}' if version else '')})
    for supplement in dossier.supplements.defer('raw_gzip', 'text'):
        rows.append({'title': supplement.title, 'source': '人工补充', 'category': 'manual', 'kind': '补充文件／链接',
            'period': supplement.period, 'published': str(supplement.published_at or ''), 'fetched': supplement.created_at,
            'layer': 'original', 'layer_label': '原始', 'summary': supplement.summary or supplement.extraction_note,
            'status': supplement.extraction_note, 'url': reverse('investment_research:supplement_read', args=[dossier.pk, supplement.pk])})
    return rows


def table_context(request, dossier, materials, overview):
    rows = rows_for(dossier, materials, overview)
    kind_options = sorted({r['kind'] for r in rows})
    query = request.GET.get('q', '').strip()[:200]
    category = request.GET.get('category', 'all')
    kind = request.GET.get('kind', '')
    layer = request.GET.get('layer', '')
    date_field = request.GET.get('date_field', 'fetched')
    if date_field not in {'fetched', 'published', 'period'}:
        date_field = 'fetched'
    dates = {}
    for key in ['from', 'to']:
        try:
            dates[key] = parse_date(request.GET.get(key, ''))
        except ValueError:
            dates[key] = None
    def row_date(row):
        value = row.get(date_field)
        if date_field == 'fetched':
            return value.date() if value else None
        try:
            return parse_date(str(value)[:10]) if value else None
        except ValueError:
            return None
    filtered = []
    for row in rows:
        if category in dict(SOURCES) and category != 'all' and row['category'] != category:
            continue
        if kind and row['kind'] != kind or layer in {'original', 'prepared'} and row['layer'] != layer:
            continue
        if query and query.lower() not in (row['title'] + ' ' + row['summary']).lower():
            continue
        date = row_date(row)
        if dates['from'] and (not date or date < dates['from']) or dates['to'] and (not date or date > dates['to']):
            continue
        filtered.append(row)
    ascending = request.GET.get('order') == 'oldest'
    known = sorted((r for r in filtered if row_date(r)), key=lambda r: (row_date(r), r['title']), reverse=not ascending)
    unknown = [r for r in filtered if not row_date(r)]
    page = Paginator(known + unknown, 20).get_page(request.GET.get('page'))
    query_args = request.GET.copy()
    query_args.pop('page', None)
    return {'material_page': page, 'material_total': len(filtered), 'table_query': query, 'category': category,
            'kind_filter': kind, 'layer_filter': layer, 'source_options': SOURCES, 'kind_options': kind_options,
            'date_field': date_field, 'date_from': request.GET.get('from', ''), 'date_to': request.GET.get('to', ''),
            'order': 'oldest' if ascending else 'newest', 'pager_query': query_args.urlencode()}
