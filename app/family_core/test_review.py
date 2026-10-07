from datetime import date, timedelta
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import RequestFactory, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import resolve, reverse
from django.utils import timezone
from family_core.content_search import search_content
from family_core.context_processors import page_navigation
from family_core.models import Family, FamilyMember
from family_core.review import weekly_review
from investment_research.models import ResearchDossier, ResearchQuestion
from notes.models import InvestmentNote, InvestmentNoteType
from portfolio.models import PortfolioSnapshot, Security
from reading.models import Book, ReadingPosition


class ReviewTests(TestCase):
    def setUp(self):
        self.family = Family.objects.create(name='回顾家庭')
        self.user = get_user_model().objects.create_user('reviewer')
        self.member = FamilyMember.objects.create(family=self.family, user=self.user, display_name='本人', role='admin')
        self.other = FamilyMember.objects.create(family=self.family, display_name='另一成员')
        self.client.force_login(self.user)
        self.security = Security.objects.create(symbol='REVIEW', name='回顾公司', market='US')
        self.dossier = ResearchDossier.objects.create(family=self.family, owner=self.member, security=self.security)
        self.other_dossier = ResearchDossier.objects.create(family=self.family, owner=self.other, security=self.security)
        self.note_type = InvestmentNoteType.objects.create(name='回顾', code='review-test')

    def test_review_empty_states_and_gets_do_not_write(self):
        for name in ['review', 'search', 'financial_basis', 'changes']:
            with self.subTest(name=name), CaptureQueriesContext(connection) as captured:
                response = self.client.get(reverse('family_core:' + name), {'q': '回顾'})
                self.assertEqual(response.status_code, 200)
            writes = [q['sql'] for q in captured if q['sql'].lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE'))]
            self.assertEqual(writes, [])

    def test_incomplete_snapshot_hides_change_despite_complete_flag(self):
        PortfolioSnapshot.objects.create(family=self.family, snapshot_date=date(2026, 10, 4), total_asset=100, extra_data={'complete': True})
        PortfolioSnapshot.objects.create(family=self.family, snapshot_date=date(2026, 10, 7), total_asset=120,
            extra_data={'complete': True, 'missing_prices': [{'security_id': 1}]})
        data = weekly_review(self.member, date(2026, 10, 7))
        self.assertFalse(data['snapshot_complete'])
        self.assertIsNone(data['asset_change'])

    def test_weekly_change_uses_family_snapshot_only_and_decimal(self):
        PortfolioSnapshot.objects.create(family=self.family, snapshot_date=date(2026, 10, 4), total_asset=Decimal('100.1234'), extra_data={'complete': True})
        PortfolioSnapshot.objects.create(family=self.family, snapshot_date=date(2026, 10, 7), total_asset=Decimal('120.2345'), extra_data={'complete': True})
        PortfolioSnapshot.objects.create(family=self.family, member=self.member, snapshot_date=date(2026, 10, 7), total_asset=999, extra_data={'complete': True})
        data = weekly_review(self.member, date(2026, 10, 7))
        self.assertEqual(data['asset_change'], Decimal('20.1111'))
        self.assertEqual(data['week_start'], date(2026, 10, 5))

    def test_search_keeps_other_members_private_research_books_and_notes_out(self):
        ResearchQuestion.objects.create(dossier=self.dossier, title='回顾本人问题')
        ResearchQuestion.objects.create(dossier=self.other_dossier, title='回顾私密问题')
        for owner, visibility, title in [(self.member, 'private', '回顾本人书'), (self.other, 'private', '回顾私密书'), (self.other, 'family', '回顾共享书')]:
            Book.objects.create(family=self.family, owner=owner, visibility=visibility, title=title)
            InvestmentNote.objects.create(family=self.family, member=owner, visibility=visibility, title=title+'笔记', content='回顾', note_type=self.note_type)
        response = self.client.get(reverse('family_core:search'), {'q': '回顾'})
        for title in ['回顾本人问题', '回顾本人书', '回顾共享书', '回顾本人书笔记']:
            self.assertContains(response, title)
        for title in ['回顾私密问题', '回顾私密书', '回顾私密书笔记']:
            self.assertNotContains(response, title)

    def test_weekly_change_requires_matching_currency(self):
        PortfolioSnapshot.objects.create(family=self.family, snapshot_date=date(2026, 10, 4), total_asset=100, currency='USD', extra_data={'complete': True})
        PortfolioSnapshot.objects.create(family=self.family, snapshot_date=date(2026, 10, 7), total_asset=800, currency='CNY', extra_data={'complete': True})
        self.assertIsNone(weekly_review(self.member, date(2026, 10, 7))['asset_change'])

    def test_search_pagination_and_safe_return_preserve_query(self):
        for i in range(25):
            Book.objects.create(family=self.family, owner=self.member, title=f'回顾书 {i:02}')
        response = self.client.get(reverse('family_core:search'), {'q': '回顾书', 'group': 'reading', 'page': 2})
        group = response.context['groups'][0]
        self.assertEqual(group['count'], 25)
        self.assertEqual(len(group['items']), 5)
        target = group['items'][0]['url']
        return_to = parse_qs(urlsplit(target).query)['return_to'][0]
        request = RequestFactory().get(target)
        request.resolver_match = resolve(request.path)
        self.assertEqual(page_navigation(request)['search_return_url'], return_to)
        self.assertIn('page=2', return_to)
        unsafe = RequestFactory().get(request.path, {'return_to': '//evil.example/'})
        unsafe.resolver_match = request.resolver_match
        self.assertNotIn('search_return_url', page_navigation(unsafe))

    def test_reading_progress_belongs_to_current_member_and_visible_book(self):
        book = Book.objects.create(family=self.family, owner=self.other, title='私密书')
        ReadingPosition.objects.create(book=book, member=self.member, file_hash='a'*64, progress=100)
        self.assertEqual(weekly_review(self.member)['reading_positions'], [])

    def test_amount_review_threshold_handles_both_signs_without_hiding_missing_prices(self):
        from .financial_basis import financial_basis
        from ledger.models import BankAccount
        from portfolio.models import InvestmentAccount
        account = InvestmentAccount.objects.create(bank_account=BankAccount.objects.create(
            family=self.family, member=self.member, account_name='阈值验证', supports_investment=True))
        snapshot = PortfolioSnapshot.objects.create(family=self.family, snapshot_date=timezone.localdate(),
            total_asset=1000, total_cash=1000, extra_data={'complete': True})
        account_snapshot = PortfolioSnapshot.objects.create(family=self.family, account=account,
            snapshot_date=snapshot.snapshot_date, total_asset=1000)
        for amount in ['0.0002', '-0.0002', '49.9999', '-49.9999', '50', '-50', '120']:
            with self.subTest(amount=amount):
                difference = Decimal(amount)
                account_snapshot.total_asset = Decimal('1000') - difference
                account_snapshot.save(update_fields=['total_asset'])
                snapshot.total_cash = Decimal('1000') - difference
                snapshot.save(update_fields=['total_cash'])
                issues = financial_basis(self.member)['issues']
                self.assertEqual(len(issues), 2 if abs(difference) >= 50 else 0)
                if issues:
                    self.assertEqual([issue['amount'] for issue in issues], [difference, difference])
                snapshot.refresh_from_db()
                self.assertEqual(snapshot.total_asset, Decimal('1000'))
        snapshot.extra_data = {'complete': True, 'missing_prices': [{'security_id': self.security.pk}]}
        snapshot.total_cash = Decimal('1000')
        snapshot.save()
        account_snapshot.total_asset = Decimal('999.9998')
        account_snapshot.save()
        self.assertEqual([issue['label'] for issue in financial_basis(self.member)['issues']], ['缺少价格'])
