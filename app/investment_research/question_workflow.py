"""Owner-confirmed questions, immutable revisions and provider-bound settings."""
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

from .models import (ResearchDossier, ResearchQuestion, ResearchQuestionRevision,
                     ResearchAutoDigestConsent, ResearchWorkflowSettings)
from .preparation import authorize
from .research_ai import ResearchAiError, report_policy

QUESTION_FIELDS = ('title', 'supporting_condition', 'reconsidering_condition', 'metrics', 'source_notes')
QUESTION_LIMITS = {'title': 600, 'supporting_condition': 1600, 'reconsidering_condition': 1600,
                   'metrics': 1600, 'source_notes': 1600}


def snapshot(question):
    return {key: getattr(question, key) for key in QUESTION_FIELDS} | {'status': question.status}


def clean_question(values):
    cleaned = {}
    for key, maximum in QUESTION_LIMITS.items():
        value = values.get(key, '')
        if not isinstance(value, str) or len(value.strip()) > maximum:
            raise ResearchAiError('问题不超过 600 字，证据条件、指标与来源各不超过 1,600 字。')
        cleaned[key] = value.strip()
    if not cleaned['title']:
        raise ResearchAiError('请填写待跟踪问题。')
    return cleaned


def visible_questions(dossier):
    return dossier.research_questions.exclude(status='removed')


def active_questions(dossier):
    return dossier.research_questions.filter(status='tracking')


@transaction.atomic
def save_question(actor, dossier, values, *, question_id=None, expected_revision=None,
                  expected_list_revision=None, introduction=None):
    authorize(actor, dossier)
    values = clean_question(values)
    locked = ResearchDossier.objects.select_for_update().get(pk=dossier.pk)
    if expected_list_revision is not None and str(locked.question_list_revision) != str(expected_list_revision):
        raise ResearchAiError('问题清单刚刚更新，请刷新后再保存。')
    question = locked.research_questions.filter(pk=question_id).first() if question_id else None
    if question_id and (not question or question.status == 'removed'):
        raise ResearchAiError('问题不在当前清单中。')
    if question and str(question.revision) != str(expected_revision):
        raise ResearchAiError('问题已在其他页面修改，请刷新后再保存。')
    duplicates = visible_questions(locked).filter(title=values['title'])
    if question:
        duplicates = duplicates.exclude(pk=question.pk)
    if duplicates.exists():
        raise ResearchAiError('清单已有同名问题，请编辑原问题，避免重复。')
    if not question and visible_questions(locked).count() >= 30:
        raise ResearchAiError('当前清单已有 30 个问题，请先移出不再关注的问题。')
    if not question and active_questions(locked).count() >= 10:
        raise ResearchAiError('当前已有 10 个跟踪中的问题，请先解决或暂停部分问题。')
    if introduction and (introduction.member_id != actor.pk or introduction.family_id != actor.family_id
            or introduction.scope.get('dossier_id') != dossier.pk
            or introduction.analysis_type != 'company_introduction' or introduction.status != 'success'):
        raise ResearchAiError('初识报告不属于当前档案。')
    question = question or ResearchQuestion(dossier=locked, position=visible_questions(locked).count(),
                                            introduction=introduction)
    changed = not question.pk or any(getattr(question, key) != value for key, value in values.items())
    if not changed:
        return question
    if question.pk:
        question.revision += 1
    for key, value in values.items():
        setattr(question, key, value)
    question.save()
    ResearchQuestionRevision.objects.create(question=question, number=question.revision,
                                             content=snapshot(question), created_by=actor)
    locked.question_list_revision += 1
    entering = not locked.question_workflow
    locked.question_workflow = True
    locked.research_paused = False
    locked.save(update_fields=['question_list_revision', 'question_workflow', 'research_paused', 'updated_at'])
    if entering:
        ResearchAutoDigestConsent.objects.filter(dossier=locked, revoked_at__isnull=True).update(revoked_at=timezone.now())
    return question


@transaction.atomic
def set_question_status(actor, dossier, question_id, status, expected_revision, expected_list_revision=None):
    authorize(actor, dossier)
    if status not in {'tracking', 'resolved', 'paused', 'removed'}:
        raise ResearchAiError('问题状态无效。')
    locked = ResearchDossier.objects.select_for_update().get(pk=dossier.pk)
    if expected_list_revision is not None and str(locked.question_list_revision) != str(expected_list_revision):
        raise ResearchAiError('问题清单刚刚更新，请刷新后再修改状态。')
    question = locked.research_questions.filter(pk=question_id).first()
    if not question or str(question.revision) != str(expected_revision):
        raise ResearchAiError('问题已更新，请刷新后再修改状态。')
    if question.status == status:
        return question
    if status == 'tracking' and active_questions(locked).exclude(pk=question.pk).count() >= 10:
        raise ResearchAiError('同时最多跟踪 10 个问题，请先解决或暂停部分问题。')
    previous = question.status
    question.status = status
    # A status change does not invalidate the answer to the same question.
    question.save(update_fields=['status', 'updated_at'])
    locked.question_list_revision += 1
    locked.question_workflow = True
    locked.save(update_fields=['question_list_revision', 'question_workflow', 'updated_at'])
    # State actions are audited separately from content revisions.
    from .models import ResearchQuestionAction
    ResearchQuestionAction.objects.create(question=question, created_by=actor,
                                           previous_status=previous, status=status)
    return question


@transaction.atomic
def start_tracking(actor, dossier, expected_list_revision):
    authorize(actor, dossier)
    locked = ResearchDossier.objects.select_for_update().get(pk=dossier.pk)
    if str(locked.question_list_revision) != str(expected_list_revision):
        raise ResearchAiError('问题清单刚刚更新，请刷新后开始跟踪。')
    if not active_questions(locked).exists():
        raise ResearchAiError('请先确认至少一个跟踪中的问题。')
    locked.is_watched = True
    locked.question_workflow = True
    locked.research_paused = False
    locked.save(update_fields=['is_watched', 'question_workflow', 'research_paused', 'updated_at'])
    # A former judgment-based consent must not continue sending obsolete content.
    from investment_watch.models import WatchConsent
    WatchConsent.objects.filter(dossier=locked, active=True).update(active=False)
    # Material recall still uses the public issuer identity; question content stays private.
    from investment_watch.models import WatchRule
    rule, created = WatchRule.objects.get_or_create(dossier=locked, defaults={
        'aliases': [locked.security.name, locked.security.symbol], 'topics': [], 'include': [], 'exclude': []})
    if not rule.enabled:
        rule.enabled = True
        rule.save(update_fields=['enabled', 'updated_at'])
    return locked


def latest_introduction(dossier):
    from .preparation import history
    return history(dossier).filter(status='success').select_related('result', 'provider').first()


def settings_for(actor):
    return ResearchWorkflowSettings.objects.filter(owner=actor).select_related('provider').first()


def money(value, label):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ResearchAiError(f'{label}应为有效金额。') from exc
    if not number.is_finite() or number <= 0 or number > 100 or number.as_tuple().exponent < -4:
        raise ResearchAiError(f'{label}须大于零、不超过 100 美元，最多四位小数。')
    return number


@transaction.atomic
def save_settings(actor, dossier, values, provider, expected_revision):
    authorize(actor, dossier)
    type(actor).objects.select_for_update().get(pk=actor.pk)
    current = settings_for(actor)
    if str(current.revision if current else 0) != str(expected_revision):
        raise ResearchAiError('研究设置已更新，请刷新后再保存。')
    budget = money(values.get('per_call_budget_usd'), '单次费用上限')
    daily = money(values.get('daily_budget_usd'), '每日自动分析上限')
    if provider:
        policy = report_policy(provider)
        if budget > policy['max_cost']:
            raise ResearchAiError('单次上限不能超过该模型已批准的投研费用上限。')
    row = current or ResearchWorkflowSettings(owner=actor, revision=0)
    for field, limit in [('preferences', 3000), ('introduction_prompt', 6000),
                         ('question_prompt', 6000), ('tracking_prompt', 6000)]:
        text = values.get(field, '')
        if not isinstance(text, str) or len(text) > limit:
            raise ResearchAiError('通用偏好不超过 3,000 字，高级提示词各不超过 6,000 字。')
        setattr(row, field, text.strip())
    row.provider = provider
    row.per_call_budget_usd, row.daily_budget_usd = budget, daily
    row.revision += 1
    row.save()
    return row


@transaction.atomic
def set_tracking_consent(actor, dossier, enabled, provider, daily_budget):
    authorize(actor, dossier)
    locked = ResearchDossier.objects.select_for_update().get(pk=dossier.pk)
    if not enabled:
        ResearchAutoDigestConsent.objects.filter(dossier=locked, revoked_at__isnull=True).update(revoked_at=timezone.now())
        return None
    if not locked.question_workflow or not locked.is_watched or not active_questions(locked).exists():
        raise ResearchAiError('请先确认问题清单并开始跟踪。')
    if not provider:
        raise ResearchAiError('请选择获准使用的模型。')
    report_policy(provider)
    from investment_watch.analysis import provider_signature
    return ResearchAutoDigestConsent.objects.update_or_create(dossier=locked, defaults={
        'provider': provider, 'authorized_by': actor, 'authorized_at': timezone.now(),
        'provider_signature': provider_signature(provider),
        'revoked_at': None, 'daily_budget_usd': money(daily_budget, '每日自动分析上限')})[0]


def attach_updates(questions):
    from .report_sections import source_sections
    reports = {}
    for question in questions:
        question.latest_update = question.updates.filter(question_revision=question.revision).select_related('analysis__result').first()
        question.legacy_answer = bool(question.latest_update and question.latest_update.analysis.analysis_type == 'thesis_synthesis')
        question.source_sections = []
        origin = question.revisions.filter(number=1).values_list('content', flat=True).first() or {}
        question.legacy_kind = origin.get('legacy_origin', {}).get('kind', 'question')
        if question.latest_update:
            analysis = question.latest_update.analysis
            if question.legacy_answer:
                if analysis.pk not in reports:
                    reports[analysis.pk] = source_sections(analysis.result.result_json, analysis.scope)
                matches = [item for item in reports[analysis.pk].get('assessments', [])
                           if item.get('text') == question.title and item.get('kind') == question.legacy_kind]
                if len(matches) == 1:
                    question.source_sections = matches[0]['source_sections']
            else:
                from .question_sections import tracking_sections
                question.source_sections = tracking_sections(question, question.latest_update)
        question.old_update = question.updates.exclude(question_revision=question.revision).exists()
    return questions
