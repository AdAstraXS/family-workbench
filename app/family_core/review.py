"""A short factual weekly review; source modules retain all business ownership."""
from datetime import datetime, time, timedelta

from django.db.models import F, Q, Sum
from django.utils import timezone

from investment_research.models import ResearchQuestion, ResearchQuestionUpdate
from investment_research.permissions import accessible_dossiers
from knowledge.models import KnowledgeDocument
from knowledge.permissions import accessible_documents
from ledger.models import AssetBalanceSnapshot, ExpenseRecord, IncomeRecord
from macro.calendar import planned_events
from portfolio.account_history import family_snapshots
from portfolio.models import InvestmentTransaction
from reading.models import ReadingPlan, ReadingPosition
from reading.permissions import accessible_books
from notes.views import _accessible_notes


def weekly_review(member, today=None):
    today = today or timezone.localdate()
    start = today - timedelta(days=today.weekday())
    next_start = start + timedelta(days=7)
    since = timezone.make_aware(datetime.combine(start, time.min))
    until = timezone.make_aware(datetime.combine(today + timedelta(days=1), time.min))
    dossiers = accessible_dossiers(member)
    questions = ResearchQuestion.objects.filter(dossier__in=dossiers, status='tracking')
    updates = ResearchQuestionUpdate.objects.filter(question__in=questions, question_revision=F('question__revision'),
                 created_at__gte=since, created_at__lt=until).exclude(direction='unchanged').select_related('question__dossier__security')
    latest = family_snapshots(member.family).filter(snapshot_date__lte=today).first()
    baseline = family_snapshots(member.family).filter(snapshot_date__lt=start).first()
    from portfolio.views import _snapshot_audit_summary
    complete = bool(latest and _snapshot_audit_summary(latest)['complete'])
    comparable = bool(complete and baseline and _snapshot_audit_summary(baseline)['complete']
                      and latest.snapshot_date >= start and latest.currency == baseline.currency)
    ledger = AssetBalanceSnapshot.objects.filter(family=member.family, is_draft=False, snapshot_date__lte=today).order_by('-snapshot_date', '-created_at').first()
    records = {}
    for key, model in [('income', IncomeRecord), ('expense', ExpenseRecord)]:
        records[key] = model.objects.filter(family=member.family, updated_at__gte=since, updated_at__lt=until).filter(
            Q(member=member) | Q(visibility='family')).count()
    transactions = InvestmentTransaction.objects.filter(account__bank_account__family=member.family,
                          updated_at__gte=since, updated_at__lt=until).count()
    books = accessible_books(member)
    return {'today': today, 'week_start': start, 'next_start': next_start, 'next_end': next_start+timedelta(days=6),
            'latest_snapshot': latest, 'baseline_snapshot': baseline, 'snapshot_complete': complete,
            'asset_change': latest.total_asset - baseline.total_asset if comparable else None,
            'ledger_snapshot': ledger,
            'ledger_total': ledger.entries.aggregate(total=Sum('base_amount'))['total'] if ledger else None,
            'record_counts': records, 'transaction_count': transactions,
            'question_updates': list(updates.order_by('-created_at', '-pk')[:8]),
            'tracked_questions': questions.count(),
            'reading_positions': list(ReadingPosition.objects.filter(member=member, book__in=books,
                    book__file__status='ready', completed_at__isnull=True, progress__lt=10000).select_related('book').order_by('-updated_at')[:4]),
            'revisit_notes': list(_accessible_notes(member).filter(member=member, note_date__lt=start).order_by('-note_date', '-pk')[:3]),
            'recent_knowledge': list(accessible_documents(member).filter(library_tier=KnowledgeDocument.LIBRARY_KNOWLEDGE,
                    updated_at__gte=since, updated_at__lt=until).order_by('-updated_at')[:5]),
            'next_plans': list(ReadingPlan.objects.filter(member=member, target_date__range=(next_start, next_start+timedelta(days=6)))[:5]),
            'next_events': planned_events(next_start, next_start+timedelta(days=6))[:8]}
