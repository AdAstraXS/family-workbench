"""Keep source-specific answers distinct from a combined conclusion."""


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
