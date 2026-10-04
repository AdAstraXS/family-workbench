"""Carry the latest owner-confirmed questions and dated legacy answers forward."""
from django.db import migrations
from django.utils import timezone


def import_questions(apps, schema_editor):
    alias = schema_editor.connection.alias
    Dossier = apps.get_model('investment_research', 'ResearchDossier')
    Question = apps.get_model('investment_research', 'ResearchQuestion')
    Revision = apps.get_model('investment_research', 'ResearchQuestionRevision')
    Update = apps.get_model('investment_research', 'ResearchQuestionUpdate')
    Preparation = apps.get_model('investment_research', 'ResearchPreparation')
    Analysis = apps.get_model('ai_analysis', 'AiAnalysisRequest')
    Result = apps.get_model('ai_analysis', 'AiAnalysisResult')
    Consent = apps.get_model('investment_research', 'ResearchAutoDigestConsent')
    WatchConsent = apps.get_model('investment_watch', 'WatchConsent')
    WatchRule = apps.get_model('investment_watch', 'WatchRule')
    imported = 0
    for dossier in Dossier.objects.using(alias).select_related('current_revision').iterator():
        basis = dossier.current_revision
        preparation = None
        if basis is None:
            preparation = Preparation.objects.using(alias).filter(dossier_id=dossier.pk).order_by('-updated_at', '-pk').first()
        titles = basis.questions if basis else preparation.questions if preparation else []
        if not isinstance(titles, list) or not titles:
            continue
        scope = {'scope__thesis_revision_id': basis.pk} if basis else {
            'scope__thesis_revision_id__isnull': True, 'scope__preparation_id': preparation.pk,
            'scope__preparation_revision': preparation.revision}
        analysis = Analysis.objects.using(alias).filter(member_id=dossier.owner_id, family_id=dossier.family_id,
            module='investment_research', analysis_type='thesis_synthesis', status='success',
            scope__dossier_id=dossier.pk, **scope).only('id', 'created_at', 'finished_at').order_by('-created_at', '-pk').first()
        result = Result.objects.using(alias).filter(request_id=analysis.pk).first() if analysis else None
        assessments = result.result_json.get('assessments', []) if result and isinstance(result.result_json, dict) else []
        if not isinstance(assessments, list):
            assessments = []
        added = 0
        for index, title in enumerate(titles):
            if not isinstance(title, str) or not title.strip() or len(title.strip()) > 600:
                continue
            title = title.strip()
            existing = Question.objects.using(alias).filter(dossier_id=dossier.pk)
            # Never resurrect removed questions or overwrite newer user edits.
            if existing.filter(title=title).exists() or existing.exclude(status='removed').count() >= 30:
                continue
            origin = {'thesis_revision_id': basis.pk} if basis else {'preparation_id': preparation.pk,
                                                                     'preparation_revision': preparation.revision}
            question = Question.objects.using(alias).create(dossier_id=dossier.pk, title=title,
                position=existing.count(), status='tracking' if existing.filter(status='tracking').count() < 10 else 'paused',
                source_notes='沿用最近一次本人确认的问题；原判断、历史分析和引用保留在旧流程历史记录中。')
            Revision.objects.using(alias).create(question_id=question.pk, number=1, created_by_id=dossier.owner_id,
                content={'title': title, 'supporting_condition': '', 'reconsidering_condition': '', 'metrics': '',
                         'source_notes': question.source_notes, 'status': question.status, 'legacy_origin': origin})
            matches = [item for item in assessments if isinstance(item, dict) and item.get('kind') == 'question'
                       and item.get('index') == index and isinstance(item.get('text', title), str)
                       and item.get('text', title).strip() == title]
            if analysis and len(matches) == 1:
                item = matches[0]
                answer = '\n'.join(item[key] for key in ('reason', 'detail', 'implication')
                                   if isinstance(item.get(key), str) and item[key])
                if answer:
                    verdict = {'supports': '有支持', 'weakens': '有反证', 'mixed': '存在不同依据', 'unknown': '证据不足'}.get(item.get('verdict'), '待核对')
                    answer = f'当时结论：{verdict}\n' + answer
                    original_date = analysis.finished_at or analysis.created_at
                    update = Update.objects.using(alias).create(question_id=question.pk, question_revision=1,
                        analysis_id=analysis.pk, answer=answer,
                        change='沿用历史分析，尚未进行新问题核查。', direction='unresolved' if item.get('verdict') in {
                            'unknown', 'mixed', 'weakens'} else 'unchanged',
                        gap=item.get('boundary', '') if item.get('verdict') in {'unknown', 'mixed'} and isinstance(item.get('boundary', ''), str) else '',
                        evidence=[{'id': '历史分析', 'title': '原分析与引用证据', 'date': timezone.localtime(original_date).strftime('%Y-%m-%d %H:%M'),
                                   'url': f'/research/{dossier.pk}/analysis/{analysis.pk}/',
                                   'source_note': '生成时的原资料、引用及核查范围保留在历史报告中。', 'text': item.get('boundary', '')}])
                    Update.objects.using(alias).filter(pk=update.pk).update(created_at=original_date)
            added += 1
        if added:
            Dossier.objects.using(alias).filter(pk=dossier.pk).update(question_workflow=True, is_watched=True,
                question_list_revision=dossier.question_list_revision + added)
            Consent.objects.using(alias).filter(dossier_id=dossier.pk, revoked_at__isnull=True).update(revoked_at=timezone.now())
            WatchConsent.objects.using(alias).filter(dossier_id=dossier.pk, active=True).update(active=False)
            WatchRule.objects.using(alias).get_or_create(dossier_id=dossier.pk,
                defaults={'aliases': [], 'topics': [], 'include': [], 'exclude': []})
            imported += added
    print(f'Imported {imported} latest owner-confirmed research questions; legacy originals preserved.')


class Migration(migrations.Migration):
    dependencies = [
        ('investment_research', '0020_researchautodigestconsent_provider_signature'),
        ('investment_watch', '0011_source_health'),
    ]
    operations = [migrations.RunPython(import_questions, migrations.RunPython.noop)]
