from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from family_core.models import AssetCategory
from ledger.models import ExpenseRecord
from portfolio import tests as fixtures
from portfolio.models import Security, InvestmentPosition, InvestmentCashMovement, InvestmentTransaction


class PortfolioPageEfficiencyTests(TestCase):
    setUp = fixtures.PortfolioOverviewTests.setUp

    def test_expense_pagination_preserves_filtered_totals(self):
        ExpenseRecord.objects.bulk_create([
            ExpenseRecord(family=self.member.family, member=self.member,
                expense_date=date(2026, 10, 4), amount=Decimal('10'), currency='CNY')
            for _ in range(65)
        ])
        url = reverse('ledger:expense_month_detail', args=[2026, 10])
        for number, count in [(1, 30), (2, 30), (3, 5)]:
            response = self.client.get(url, {'member': self.member.pk, 'table_page_expenses': number})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content.decode().count('<tr class="expense-record-row">'), count)
            self.assertEqual(len(response.context['expense_rows']), 65)
            self.assertEqual(response.context['expense_family_total'], Decimal('650'))
            self.assertContains(response, f'member={self.member.pk}')

    def test_missing_price_is_not_reported_as_missing_fx(self):
        self.latest_position.current_price = Decimal('0')
        self.latest_position.save(update_fields=['current_price'])
        response = self.client.get(reverse('portfolio:overview'))
        self.assertContains(response, '已知部分估值')
        self.assertContains(response, '缺少价格')
        self.assertFalse(response.context['missing_rates'])
        self.assertNotContains(response, '部分资产缺少兑')

    def test_account_missing_price_does_not_claim_missing_fx(self):
        self.latest_position.current_price = Decimal('0')
        self.latest_position.save(update_fields=['current_price'])
        response = self.client.get(reverse('portfolio:account_detail', args=[self.account.pk]))
        self.assertFalse(response.context['missing_exchange_rates'])
        self.assertTrue(response.context['valuation']['missing_prices'])
        self.assertContains(response, '缺少价格')

    def test_stale_prices_are_visible_to_ordinary_members(self):
        response = self.client.get(reverse('portfolio:overview'))
        self.assertContains(response, '估值仅供参考')
        self.assertContains(response, '价格日期')

    def test_overview_queries_do_not_grow_per_position(self):
        category = AssetCategory.objects.create(name='Performance category')
        self.security.asset_category = category
        self.security.save(update_fields=['asset_category'])
        url = reverse('portfolio:overview')
        self.client.get(url)
        with CaptureQueriesContext(connection) as small:
            self.client.get(url)
        for i in range(25):
            security = Security.objects.create(symbol=f'EFF{i}',name=f'Position {i}',currency='HKD',asset_category=category)
            InvestmentPosition.objects.create(account=self.account,security=security,quantity=1,avg_cost=10,
                current_price=12,position_date=self.latest_position.position_date)
        with CaptureQueriesContext(connection) as large:
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(len(large), len(small) + 1)
        self.assertFalse(any(q['sql'].lstrip().startswith(('INSERT', 'UPDATE', 'DELETE')) for q in large))

    def test_overview_does_not_build_individual_history(self):
        with patch('portfolio.views._individual_profit_data', side_effect=AssertionError('unneeded history')):
            response = self.client.get(reverse('portfolio:account_detail', args=[self.account.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('transactions', response.context)
        self.assertNotIn('cash_movements', response.context)

    def test_cash_pagination_retains_entire_history_balance(self):
        InvestmentCashMovement.objects.bulk_create([
            InvestmentCashMovement(account=self.account,movement_date=self.latest_position.position_date,
                movement_type='deposit',amount=i+1,currency='USD' if i % 2 else 'HKD') for i in range(110)])
        expected, balances = {}, {}
        for row in InvestmentCashMovement.objects.filter(account=self.account).order_by('movement_date','created_at','pk'):
            balances[row.currency] = balances.get(row.currency, Decimal(0)) + row.amount
            expected[row.pk] = balances[row.currency]
        url = reverse('portfolio:account_detail', args=[self.account.pk])
        for number in (1,2,3,4):
            response = self.client.get(url, {'tab':'cashflows','page':number})
            self.assertEqual(response.status_code, 200)
            for row in response.context['cash_movements']:
                self.assertEqual(row.balance_after, expected[row.pk])
        self.assertEqual(response.context['activity_page'].paginator.count, 111)

    def test_transaction_filter_and_pagination_are_applied_in_database(self):
        InvestmentTransaction.objects.bulk_create([
            InvestmentTransaction(account=self.account,security=self.security,trade_date=self.latest_position.position_date,
                trade_type='buy',quantity=1,price=10,amount=10,currency='HKD') for i in range(120)])
        response = self.client.get(reverse('portfolio:account_detail',args=[self.account.pk]), {'tab':'transactions','page':2,'stock':f'security:{self.security.pk}'})
        self.assertEqual(len(response.context['transactions']), 30)
        self.assertEqual(response.context['activity_page'].paginator.count, 120)
        self.assertLess(len(response.content), 120000)
        self.assertIn(f'stock=security%3A{self.security.pk}', response.context['pagination_query'])
