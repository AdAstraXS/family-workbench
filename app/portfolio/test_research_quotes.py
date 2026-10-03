from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pandas as pd
from django.test import TestCase
from django.utils import timezone

from .models import Security, SecurityMarketSnapshot, StockMarketResearchSnapshot
from .research_quotes import saved_research_quote, normalize_quote, freeze_research_quote
from .stock_research import fetch_stock_research


class ResearchQuoteTests(TestCase):
    def setUp(self):
        self.security = Security.objects.create(symbol='COST', name='Costco', market='US', currency='USD')

    def test_latest_dated_cache_is_shared_without_any_writes(self):
        old = timezone.now() - timedelta(days=3)
        SecurityMarketSnapshot.objects.create(security=self.security, last_price=90, price_as_of=old)
        stamp = (timezone.now() - timedelta(hours=2)).isoformat()
        StockMarketResearchSnapshot.objects.create(security=self.security, quote={'price': '100', 'as_of': stamp})
        with self.assertNumQueries(2):
            quote = saved_research_quote(self.security)
        self.assertEqual(quote['price'], Decimal('100'))
        self.assertIsNone(quote['pe_ttm'])
        self.assertEqual(SecurityMarketSnapshot.objects.get(security=self.security).last_price, Decimal('90'))

    def test_no_acquisition_timestamp_or_adjusted_bar_becomes_quote(self):
        StockMarketResearchSnapshot.objects.create(security=self.security, quote={'price': '100'},
            candles=[{'date': '2026-01-01', 'close': '200'}], fetched_at=timezone.now())
        self.assertEqual(saved_research_quote(self.security), {})
        for raw in [dict(price='NaN', as_of='2026-01-01'), dict(price='100', as_of='bad'),
                    dict(price='100', as_of='2099-01-01'),
                    dict(price='100', as_of='2026-01-01', price_type='close', adjustment='qfq')]:
            self.assertIsNone(normalize_quote(self.security, raw))

    def test_report_price_is_frozen_and_legacy_missing_stays_missing(self):
        from investment_research.valuation_trial import build_valuation_trial
        stamp = (timezone.now() - timedelta(hours=1)).isoformat()
        quote = normalize_quote(self.security, dict(price='100', as_of=stamp, pe_ttm='20'))
        scope = {'market_context': freeze_research_quote(quote)}
        StockMarketResearchSnapshot.objects.create(security=self.security,
            quote=dict(price='200', as_of=timezone.now().isoformat(), pe_ttm='20'))
        trial = build_valuation_trial(self.security, scope, {}, frozen=True)
        self.assertEqual(trial['price'], Decimal('100'))
        self.assertEqual(build_valuation_trial(self.security, scope, {})['price'], Decimal('200'))
        self.assertFalse(build_valuation_trial(self.security, {}, {}, frozen=True)['available'])

    @patch('portfolio.stock_research.socket.create_connection')
    @patch('futu.OpenQuoteContext')
    def test_quote_fallback_explicitly_requests_unadjusted_completed_days(self, context_class, connection):
        from futu import AuType
        context = context_class.return_value
        context.get_market_snapshot.return_value = (1, 'no live quote permission')
        day = (timezone.now() - timedelta(days=2)).date().isoformat()
        context.request_history_kline.return_value = (0, pd.DataFrame([
            {'time_key': day + ' 00:00:00', 'close': '100.25'},
            {'time_key': '2099-01-01 00:00:00', 'close': '900'}]), None)
        snapshot = fetch_stock_research(self.security, quote_only=True)
        self.assertEqual(snapshot.quote['price'], '100.25')
        self.assertEqual(snapshot.quote['as_of'], day)
        self.assertEqual(context.request_history_kline.call_args.kwargs['autype'], AuType.NONE)
        self.assertNotIn('pe_ttm', snapshot.quote)
        self.assertFalse(SecurityMarketSnapshot.objects.exists())
        context.get_valuation_detail.assert_not_called()
        context.close.assert_called_once()
        normalized = saved_research_quote(self.security)
        self.assertEqual(normalized['date_precision'], 'day')
        self.assertNotIn('00:00', normalized['as_of_label'])
