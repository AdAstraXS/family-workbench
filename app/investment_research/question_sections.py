"""Keep source-specific answers distinct from a combined conclusion."""


def history_sections(question, update, revisions):
    if update.analysis.analysis_type != 'thesis_synthesis':
        return tracking_sections(question, update)
    from .report_sections import source_sections
    content = revisions.get(update.question_revision, {})
    origin = revisions.get(1, {}).get('legacy_origin', {})
    report = source_sections(update.analysis.result.result_json, update.analysis.scope)
    matches = [row for row in report.get('assessments', [])
               if row.get('text') == content.get('title', question.title)
               and row.get('kind') == origin.get('kind', 'question')]
    return matches[0]['source_sections'] if len(matches) == 1 else []


def tracking_sections(question, update):
    rows = update.analysis.result.result_json.get('updates', [])
    row = next((row for row in rows if row.get('question_id') == question.pk
                and row.get('revision') == update.question_revision), {})
    evidence = {item['id']: item for item in update.analysis.sanitized_input.get('evidence', [])}
    parts = []
    for key, title in (('official_analysis', '一 · 投研资料：证据与分析'),
                       ('news_analysis', '二 · 相关新闻：证据与分析')):
        saved = row.get(key)
        refs = saved.get('refs', []) if isinstance(saved, dict) else []
        parts.append({'title': title, 'reason': saved.get('answer', '') if isinstance(saved, dict) else '',
                      'boundary': saved.get('gap', '') if isinstance(saved, dict) else '',
                      'note': '' if isinstance(saved, dict) else '此记录未保存此类资料的独立分析，不能由综合回复推算。',
                      'cited_facts': [evidence[r] for r in refs if r in evidence],
                      'citations': [evidence[r] for r in refs if r in evidence]})
    parts.append({'title': '三 · 综合分析与判断', 'reason': update.answer, 'boundary': update.gap,
                  'cited_facts': update.evidence, 'citations': update.evidence})
    return parts
