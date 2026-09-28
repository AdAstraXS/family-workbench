from decimal import Decimal
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pandas as pd
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from family_core.models import Family, FamilyMember

from .models import Security, StockMarketResearchSnapshot, WatchlistItem
from .stock_research import fetch_stock_research, number, rating_label, technical_observations


class StockResearchCalculationTests(TestCase):
    def test_invalid_numbers_do_not_become_financial_facts(self):
        self.assertIsNone(number(float("nan")))
        self.assertIsNone(number(float("inf")))
        self.assertEqual(number("27.51"), Decimal("27.51"))
        self.assertEqual(rating_label("BUY"), "买入")

    def test_indicators_need_enough_history_and_zones_need_repeated_turns(self):
        short = [{"date": "2026-01-01", "close": "100", "high": "101", "low": "99", "volume": "10"}]
        observation = technical_observations(short)
        self.assertIsNone(observation.get("ma20"))
        self.assertNotIn("support", observation)
        rows = []
        for index in range(220):
            close = Decimal("100") + Decimal(index) / 10
            rows.append({"date": f"2026-{index // 28 + 1:02d}-{index % 28 + 1:02d}",
                         "close": str(close), "high": str(close + 1),
                         "low": str(close - 1), "volume": "100"})
        observation = technical_observations(rows)
        self.assertIsNotNone(observation["ma200"])
        self.assertIsNotNone(observation["rsi14"])


class StockResearchPageTests(TestCase):
    def setUp(self):
        self.family = Family.objects.create(name="行情研究家庭")
        self.other_family = Family.objects.create(name="其他家庭")
        self.owner_user = get_user_model().objects.create_user(username="stock_owner", password="test-pass")
        self.other_user = get_user_model().objects.create_user(username="stock_other", password="test-pass")
        self.owner = FamilyMember.objects.create(family=self.family, user=self.owner_user, display_name="本人")
        FamilyMember.objects.create(family=self.other_family, user=self.other_user, display_name="他人")
        self.security = Security.objects.create(symbol="MSFT", name="微软", market="US", currency="USD")
        WatchlistItem.objects.create(family=self.family, member=self.owner, security=self.security)
        self.url = reverse("portfolio:stock_market_detail", args=[self.security.pk])
        self.refresh_url = reverse("portfolio:stock_market_refresh", args=[self.security.pk])

    @patch("portfolio.views.fetch_stock_research")
    def test_get_is_read_only_and_family_scoped(self, fetch):
        self.client.force_login(self.other_user)
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.assertEqual(self.client.post(self.refresh_url).status_code, 404)
        self.client.force_login(self.owner_user)
        self.assertEqual(self.client.get(self.url).status_code, 200)
        self.assertEqual(self.client.get(self.refresh_url).status_code, 405)
        self.assertFalse(StockMarketResearchSnapshot.objects.exists())
        fetch.assert_not_called()

    @patch("portfolio.views.fetch_stock_research")
    def test_stale_cache_autofetches_only_after_cooldown(self, fetch):
        self.client.force_login(self.owner_user)
        snapshot = StockMarketResearchSnapshot.objects.create(
            security=self.security,
            quote={"price": "493.78"},
            fetched_at=timezone.now() - timedelta(days=2),
            last_attempt_at=timezone.now() - timedelta(hours=2),
        )
        self.assertContains(self.client.get(self.url), 'data-autofetch="true"')
        snapshot.last_attempt_at = timezone.now()
        snapshot.save(update_fields=["last_attempt_at"])
        self.assertContains(self.client.get(self.url), 'data-autofetch="false"')
        fetch.assert_not_called()

    @patch("portfolio.views.fetch_stock_research")
    def test_refresh_reports_partial_failure_and_keeps_cached_view(self, fetch):
        self.client.force_login(self.owner_user)
        snapshot = StockMarketResearchSnapshot.objects.create(
            security=self.security, quote={"price": "493.78", "as_of": "2026-09-18"},
            errors={"morningstar": "暂不可用"},
        )
        snapshot._refreshed_any = True
        fetch.return_value = snapshot
        response = self.client.post(self.refresh_url)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(response.json()["errors"], {"morningstar": "暂不可用"})
        self.assertContains(self.client.get(self.url), "493.78")

    @patch("portfolio.stock_research.socket.create_connection")
    @patch("futu.OpenQuoteContext")
    def test_provider_uses_futu_history_and_keeps_only_structured_morningstar(self, context_class, connection):
        context = context_class.return_value
        context.get_market_snapshot.return_value = (0, pd.DataFrame([{
            "last_price": 493.78, "prev_close_price": 490.0, "update_time": "2026-09-18 16:00:00",
            "pe_ttm_ratio": 27.51, "total_market_val": 3000000000000,
        }]))
        context.request_history_kline.return_value = (0, pd.DataFrame([{
            "time_key": "2026-09-18 00:00:00", "open": 490, "high": 495,
            "low": 489, "close": 493.78, "volume": 1000,
        }]), None)
        context.get_valuation_detail.return_value = (0, {
            "last_update_time_str": "2026-09-18", "trend": {
                "current_value": 27.5, "average_value": 25.0, "valuation_percentile": 60,
                "historical_items": [{"time_str": "2026-09-18", "value": 27.5}],
            },
        })
        context.get_research_analyst_consensus.return_value = (0, {
            "total": 32, "rating": 4, "average": 579.77, "update_time_str": "2026-09-18",
        })
        context.get_research_rating_summary.return_value = (0, {"inst_rating_summary_list": []})
        context.get_research_morningstar_report.return_value = (0, {
            "star_rating": 4, "fair_value": 600, "star_update_time_str": "2026-09-17",
            "investment_thesis_content": "licensed full report text",
        })
        snapshot = fetch_stock_research(self.security)
        self.assertEqual(snapshot.quote["change_rate"], str((Decimal("493.78") / Decimal("490") - 1) * 100))
        self.assertEqual(snapshot.valuation["pe"]["3y"]["average"], "25.0")
        self.assertEqual(snapshot.morningstar["fair_value"], "600")
        self.assertNotIn("investment_thesis_content", snapshot.morningstar)
        self.assertEqual(context.get_valuation_detail.call_count, 9)
        context.close.assert_called_once()
        context.get_research_morningstar_report.return_value = (1, "暂不可用")
        second = fetch_stock_research(self.security)
        self.assertEqual(second.morningstar["fair_value"], "600")
        self.assertIn("morningstar", second.errors)
