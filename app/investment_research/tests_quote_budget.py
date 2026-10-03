from importlib import import_module
from types import SimpleNamespace
from unittest.mock import patch

from django.apps import apps
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.urls import reverse

from ai_analysis.models import AiProvider
from family_core.models import Family, FamilyMember
from portfolio.models import Security, StockMarketResearchSnapshot
from .services import create_exploration


class QuoteBudgetTests(TestCase):
    def test_budget_migration_only_updates_approved_existing_pro_policy(self):
        def provider(model, cap, host='https://api.deepseek.com/v1'):
            return AiProvider.objects.create(name=model + cap, model_name=model, base_url=host,
                extra_data={'allow_research_analysis': True, 'research_max_estimated_usd': cap,
                            'research_report_output_tokens': 32768, 'preserved': 'setting'})
        approved = provider('deepseek-v4-pro', '0.10')
        flash = provider('deepseek-flash', '0.10')
        custom = provider('deepseek-v4-pro', '0.25')
        different_host = provider('deepseek-v4-pro', '0.10', 'https://example.com/v1')
        run = import_module('investment_research.migrations.0018_approved_pro_report_budget').apply_budget
        run(apps, SimpleNamespace(connection=connection))
        run(apps, SimpleNamespace(connection=connection))
        for item, expected in [(approved, '0.50'), (flash, '0.10'), (custom, '0.25'), (different_host, '0.10')]:
            item.refresh_from_db()
            self.assertEqual(item.extra_data['research_max_estimated_usd'], expected)
            self.assertEqual(item.extra_data['preserved'], 'setting')
            self.assertEqual(item.extra_data['research_report_output_tokens'], 32768)

    @patch('portfolio.stock_research.fetch_stock_research')
    def test_valuation_get_and_refresh_are_private_and_separate(self, fetch):
        family = Family.objects.create(name='Quotes')
        user = get_user_model().objects.create_user('quote-owner')
        other_user = get_user_model().objects.create_user('quote-other')
        member = FamilyMember.objects.create(user=user, family=family, display_name='Owner')
        FamilyMember.objects.create(user=other_user, family=family, display_name='Other')
        security = Security.objects.create(symbol='COST', name='Costco', market='US')
        dossier = create_exploration(actor=member, security=security)
        url = reverse('investment_research:valuation', args=[dossier.pk])
        refresh = reverse('investment_research:refresh_quote', args=[dossier.pk])
        self.client.force_login(user)
        self.assertContains(self.client.get(url), '更新行情')
        self.assertEqual(self.client.get(refresh).status_code, 405)
        fetch.assert_not_called()
        self.assertFalse(StockMarketResearchSnapshot.objects.exists())
        fetch.return_value = SimpleNamespace(_refreshed_any=True)
        self.assertEqual(self.client.post(refresh).status_code, 302)
        fetch.assert_called_once_with(security, quote_only=True)
        self.client.force_login(other_user)
        self.assertEqual(self.client.post(refresh).status_code, 404)
        self.client.force_login(user)
        member.role = FamilyMember.ROLE_VIEWER
        member.save(update_fields=['role'])
        self.assertEqual(self.client.post(refresh).status_code, 403)
