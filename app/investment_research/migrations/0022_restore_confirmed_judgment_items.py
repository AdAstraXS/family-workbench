"""Complete the explicitly requested legacy import without rewriting originals."""
from django.db import migrations


def restore_judgment_items(apps, schema_editor):
    alias = schema_editor.connection.alias
    Dossier = apps.get_model('investment_research', 'ResearchDossier')
    Question = apps.get_model('investment_research', 'ResearchQuestion')
    Revision = apps.get_model('investment_research', 'ResearchQuestionRevision')
    Update = apps.get_model('investment_research', 'ResearchQuestionUpdate')
    Analysis = apps.get_model('ai_analysis', 'AiAnalysisRequest')
    Result = apps.get_model('ai_analysis', 'AiAnalysisResult')
    count = 0
    for dossier in Dossier.objects.using(alias).select_related('current_revision').iterator():
        basis = dossier.current_revision
        if not basis or not isinstance(basis.pillars, list):
            continue
        # Only complete a formal revision already carried into the new workflow.
        if not Revision.objects.using(alias).filter(question__dossier_id=dossier.pk,
                content__legacy_origin__thesis_revision_id=basis.pk).exists():
            continue
        analysis = Analysis.objects.using(alias).filter(member_id=dossier.owner_id, family_id=dossier.family_id,
            module='investment_research', analysis_type='thesis_synthesis', status='success',
            scope__dossier_id=dossier.pk, scope__thesis_revision_id=basis.pk).order_by('-created_at', '-pk').first()
        result = Result.objects.using(alias).filter(request_id=analysis.pk).first() if analysis else None
        assessments = result.result_json.get('assessments', []) if result and isinstance(result.result_json, dict) else []
        if not isinstance(assessments, list):
            assessments = []
        added = 0
        for index, title in enumerate(basis.pillars):
            if not isinstance(title, str) or not title.strip() or len(title.strip()) > 600:
                continue
            title = title.strip()
            existing = Question.objects.using(alias).filter(dossier_id=dossier.pk)
            if existing.filter(title=title).exists() or existing.exclude(status='removed').count() >= 30:
                continue
            question = Question.objects.using(alias).create(dossier_id=dossier.pk, title=title,
                position=max(existing.values_list('position', flat=True), default=-1) + 1,
                status='tracking' if existing.filter(status='tracking').count() < 10 else 'paused',
                source_notes='沿用本人已确认的持有判断，保留原陈述，不将其改写为新的事实结论。')
            Revision.objects.using(alias).create(question_id=question.pk, number=1, created_by_id=dossier.owner_id,
                content={'title': title, 'status': question.status, 'source_notes': question.source_notes,
                         'supporting_condition': '', 'reconsidering_condition': '', 'metrics': '',
                         'legacy_origin': {'thesis_revision_id': basis.pk, 'kind': 'pillar', 'index': index}})
            matches = [item for item in assessments if isinstance(item, dict) and item.get('kind') == 'pillar'
                       and item.get('index') == index and item.get('text', title) == title]
            if analysis and len(matches) == 1:
                item = matches[0]
                answer = '\n'.join(item[key] for key in ('reason', 'detail', 'implication')
                                   if isinstance(item.get(key), str) and item[key])
                if answer:
                    verdict = {'supports': '有支持', 'weakens': '有反证', 'mixed': '存在不同依据', 'unknown': '证据不足'}.get(item.get('verdict'), '待核对')
                    date = analysis.finished_at or analysis.created_at
                    update = Update.objects.using(alias).create(question_id=question.pk, question_revision=1,
                        analysis_id=analysis.pk, answer=f'当时结论：{verdict}\n{answer}',
                        change='沿用历史分析，尚未进行新问题核查。', direction='unchanged',
                        gap=item.get('boundary', '') if isinstance(item.get('boundary'), str) else '',
                        evidence=[{'id': '历史分析', 'title': '原分析与引用证据',
                                   'url': f'/research/{dossier.pk}/analysis/{analysis.pk}/',
                                   'source_note': '原持有判断及其三部分分析保留在历史报告中。'}])
                    Update.objects.using(alias).filter(pk=update.pk).update(created_at=date)
            added += 1
        if added:
            Dossier.objects.using(alias).filter(pk=dossier.pk).update(question_list_revision=dossier.question_list_revision + added)
            count += added
    print(f'Restored {count} confirmed judgment items; original statements and reports preserved.')


class Migration(migrations.Migration):
    dependencies = [('investment_research', '0021_import_latest_confirmed_questions')]
    operations = [migrations.RunPython(restore_judgment_items, migrations.RunPython.noop)]
