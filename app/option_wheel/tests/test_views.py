"""Current household option screen: view safety and family ownership."""

from datetime import timedelta
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core import signing
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from family_core.models import Family, FamilyMember
from option_wheel.models import WheelAnalysisJob, WheelWatchItem


class OptionWheelPageTests(TestCase):
    def setUp(self):
        self.family = Family.objects.create(name="wheel-page")
        self.admin = get_user_model().objects.create_user(
            username="wheel-admin", is_superuser=True, is_staff=True,
        )
        self.member = get_user_model().objects.create_user(username="wheel-member")
        FamilyMember.objects.create(family=self.family, user=self.admin, display_name="我")
        FamilyMember.objects.create(family=self.family, user=self.member, display_name="家庭成员")
        self.client.force_login(self.admin)

    def test_login_is_required(self):
        self.client.logout()
        response = self.client.get(reverse("option_wheel:index"))
        self.assertRedirects(response, f"{reverse('login')}?next={reverse('option_wheel:index')}")

    def test_home_contains_current_flow_without_capacity_controls(self):
        WheelWatchItem.objects.create(family=self.family, symbol="INTC", name="Intel")
        response = self.client.get(reverse("option_wheel:index"))
        self.assertContains(response, "两个账户的现金与净值")
        self.assertContains(response, "Intel")
        self.assertContains(response, "合约对比")
        for retired in ("策略总闸门", "保存为正式容量快照", "风险总闸", "当前阻断项"):
            self.assertNotContains(response, retired)

    def test_member_can_read_but_cannot_mutate_or_analyze(self):
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(reverse("option_wheel:index")).status_code, 200)
        self.assertEqual(self.client.post(reverse("option_wheel:watch_action"), {
            "action": "add", "symbol": "MU",
        }).status_code, 403)
        self.assertEqual(self.client.post(reverse("option_wheel:analyze"), {}).status_code, 403)

    def test_family_watchlist_and_history_are_isolated(self):
        other = Family.objects.create(name="other family")
        WheelWatchItem.objects.create(family=other, symbol="TSLA", name="Other Tesla")
        WheelWatchItem.objects.create(family=self.family, symbol="INTC", name="Intel")
        WheelAnalysisJob.objects.create(
            family=other, requested_by=self.admin, selection={"mode": "screening_v2", "symbols": ["TSLA"]},
            status="saved", expires_at=timezone.now() + timedelta(minutes=12),
        )
        response = self.client.get(reverse("option_wheel:index"))
        self.assertContains(response, "Intel")
        self.assertNotContains(response, "Other Tesla")
        self.assertNotContains(response, "TSLA")

    def test_get_has_no_market_or_database_side_effects(self):
        from unittest.mock import patch
        with patch("option_wheel.watch_refresh.refresh_watch_prices") as prices, patch(
            "option_wheel.jobs.fetch_probe"
        ) as probe:
            before = WheelWatchItem.objects.count()
            response = self.client.get(reverse("option_wheel:index"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(WheelWatchItem.objects.count(), before)
        prices.assert_not_called()
        probe.assert_not_called()

    def test_admin_can_add_and_remove_watch_symbol(self):
        add = self.client.post(reverse("option_wheel:watch_action"), {"action": "add", "symbol": "MU"})
        self.assertEqual(add.status_code, 302)
        self.assertTrue(WheelWatchItem.objects.filter(family=self.family, symbol="MU").exists())
        remove = self.client.post(reverse("option_wheel:watch_action"), {"action": "remove", "symbol": "MU"})
        self.assertEqual(remove.status_code, 302)
        self.assertFalse(WheelWatchItem.objects.filter(family=self.family, symbol="MU").exists())

    def test_analysis_rejects_stock_outside_family_watchlist(self):
        key = uuid4()
        token = signing.dumps({"family": self.family.pk, "key": str(key)}, salt="wheel-live-job-v1")
        response = self.client.post(reverse("option_wheel:analyze"), {
            "request_token": token, "symbols": ["MU"], "expiry_choice": "next",
            "premium_min": "100", "premium_max": "500",
        })
        self.assertEqual(response.status_code, 400)
        self.assertFalse(WheelAnalysisJob.objects.filter(pk=key).exists())
