from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from django.test import SimpleTestCase
from .views import IpoProfitEvent, profit_totals, single_currency_profit


class IpoCurrencySummaryTests(SimpleTestCase):
    def test_original_currency_profits_are_never_added_together(self):
        events = [IpoProfitEvent(ipo_trade=SimpleNamespace(listing=SimpleNamespace(stock_code=code)),
                  event_date=date(2026,10,1),profit=Decimal(amount))
                  for code,amount in [('00700.HK','100'),('TEST.US','100'),('00001','-10')]]
        self.assertEqual(profit_totals(events), [{'currency':'HKD','amount':Decimal('90')}, {'currency':'USD','amount':Decimal('100')}])
        self.assertIsNone(single_currency_profit(events))

    def test_transaction_currency_is_authoritative(self):
        event = IpoProfitEvent(ipo_trade=SimpleNamespace(listing=SimpleNamespace(stock_code='TEST.US')),
            event_date=date(2026,10,1),profit=Decimal('12'),transaction=SimpleNamespace(currency='EUR'))
        self.assertEqual(profit_totals([event]), [{'currency':'EUR','amount':Decimal('12')}])
