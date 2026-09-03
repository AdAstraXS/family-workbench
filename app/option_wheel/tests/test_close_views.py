from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core import signing
from django.core.exceptions import ValidationError
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from family_core.models import Family, FamilyMember
from ledger.models import BankAccount
from portfolio.models import InvestmentAccount, Security
from option_wheel.close_data import CloseDataError, MODE
from option_wheel.close_views import SALT
from option_wheel.models import DataStatus, WheelBrokerAccountSnapshot, WheelCloseReport, WheelDecision, WheelPolicy


def evidence():
    return {"mode": MODE, "execution_allowed": False, "symbol": "TSLA", "target_date": "2026-09-02",
            "candidates": [], "issues": [], "excluded": [], "accounts": []}


class ClosePageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.family = Family.objects.create(name="Close Family")
        cls.user = get_user_model().objects.create_user(username="close-admin", is_superuser=True, is_staff=True)
        cls.member = FamilyMember.objects.create(family=cls.family, user=cls.user, display_name="我")
        cls.stock = Security.objects.create(symbol="TSLA", market="US", currency="USD", asset_type=Security.TYPE_STOCK)
        bank = BankAccount.objects.create(family=cls.family, member=cls.member, account_name="盈透证券", supports_investment=True)
        cls.account = InvestmentAccount.objects.create(bank_account=bank)
        cls.policy = WheelPolicy.objects.create(family=cls.family, account=cls.account, underlying=cls.stock)

    def setUp(self):
        self.client.force_login(self.user)
        self.url = reverse("option_wheel:close_refresh")
        self.token = signing.dumps({"family": self.family.pk, "key": str(uuid4())}, salt=SALT)
        self.body = {"symbols": "TSLA", "request_token": self.token, "confirm_read_only": "yes"}

    def create_report(self, family=None):
        return WheelCloseReport.objects.create(family=family or self.family, symbol="TSLA", target_date=date(2026, 9, 2),
                                               request_key=uuid4(), evidence=evidence())

    @patch("option_wheel.close_views.fetch_close_report")
    def test_get_is_read_only_and_exposes_navigation(self, fetch):
        report = self.create_report()
        page = self.client.get(reverse("option_wheel:close_index"))
        self.assertContains(page, "收盘观察清单")
        self.assertContains(page, "历史成交参考价不是当前可收权利金")
        self.assertContains(page, 'data-analysis-mode="close"')
        self.assertContains(self.client.get(reverse("option_wheel:close_detail", args=[report.pk])), "未形成策略建议")
        self.assertEqual(self.client.get(self.url).status_code, 405)
        fetch.assert_not_called()
        self.assertEqual(WheelCloseReport.objects.count(), 1)

    @patch("option_wheel.close_views.fetch_close_report", side_effect=lambda _: evidence())
    def test_post_saves_separately_and_repeat_does_not_fetch(self, fetch):
        result = self.client.post(self.url, self.body, HTTP_ACCEPT="application/json")
        self.assertEqual(result.json()["outcome"], "saved")
        report = WheelCloseReport.objects.get()
        self.assertFalse(report.evidence["accounts"][0]["ready_at_collection"])
        self.assertIsNone(report.evidence["accounts"][0]["snapshot_id"])
        self.assertEqual(WheelDecision.objects.count(), 0)
        second = self.client.post(self.url, self.body, HTTP_ACCEPT="application/json")
        self.assertEqual(second.json()["outcome"], "saved")
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(WheelCloseReport.objects.count(), 1)

    @patch("option_wheel.close_views.fetch_close_report")
    def test_confirmation_token_and_symbol_checked_before_fetch(self, fetch):
        for change in ({"confirm_read_only": ""}, {"request_token": "bad"}, {"symbols": "AMD"},
                       {"symbols": ["TSLA", "NVDA"]}):
            self.assertEqual(self.client.post(self.url, {**self.body, **change}).status_code, 400)
        fetch.assert_not_called()

    @patch("option_wheel.close_views.fetch_close_report")
    def test_foreign_family_token_and_disabled_policy_rejected(self, fetch):
        token = signing.dumps({"family": self.family.pk + 100, "key": str(uuid4())}, salt=SALT)
        self.assertEqual(self.client.post(self.url, {**self.body, "request_token": token}).status_code, 400)
        self.policy.enabled = False
        self.policy.save()
        self.assertEqual(self.client.post(self.url, self.body).status_code, 400)
        fetch.assert_not_called()

    @patch("option_wheel.close_views.fetch_close_report", side_effect=lambda _: evidence())
    def test_expired_capacity_is_not_refreshed_by_observation(self, fetch):
        as_of = timezone.now() - timedelta(days=2)
        snapshot = WheelBrokerAccountSnapshot.objects.create(
            family=self.family, account=self.account, source_kind=WheelBrokerAccountSnapshot.SOURCE_MANUAL_FILE,
            source_reference="old-fixture", currency="USD", source_as_of=as_of, settled_cash=Decimal("10000"),
            unsettled_cash=Decimal("0"), reserved_cash=Decimal("0"), nav=Decimal("12000"),
            margin_loan_balance=Decimal("0"), uses_margin=False, positions_summary={}, open_obligations={},
            data_status=DataStatus.COMPLETE,
        )
        self.assertEqual(self.client.post(self.url, self.body).status_code, 302)
        saved = WheelCloseReport.objects.get().evidence["accounts"][0]
        self.assertFalse(saved["ready_at_collection"])
        snapshot.refresh_from_db()
        self.assertEqual(snapshot.source_as_of, as_of)
        self.assertEqual(WheelBrokerAccountSnapshot.objects.count(), 1)

    @patch("option_wheel.close_views.fetch_close_report")
    def test_members_read_only_and_other_family_hidden(self, fetch):
        report = self.create_report()
        user = get_user_model().objects.create_user(username="member")
        FamilyMember.objects.create(family=self.family, user=user, display_name="Member")
        self.client.force_login(user)
        self.assertEqual(self.client.post(self.url, self.body).status_code, 403)
        self.assertNotContains(self.client.get(reverse("option_wheel:close_index")), 'id="wheel-analysis-form"')
        self.assertEqual(self.client.get(reverse("option_wheel:close_detail", args=[report.pk])).status_code, 200)
        other = Family.objects.create(name="Other")
        foreign = self.create_report(other)
        self.assertEqual(self.client.get(reverse("option_wheel:close_detail", args=[foreign.pk])).status_code, 404)
        fetch.assert_not_called()

    @patch("option_wheel.close_views.ProbeLock")
    @patch("option_wheel.close_views.fetch_close_report")
    def test_busy_and_failed_queries_save_nothing_and_release_lock(self, fetch, lock):
        lock.return_value.acquire.return_value = False
        response = self.client.post(self.url, self.body, HTTP_ACCEPT="application/json")
        self.assertEqual(response.json()["outcome"], "not_saved")
        fetch.assert_not_called()
        lock.return_value.acquire.return_value = True
        fetch.side_effect = CloseDataError("查询失败")
        response = self.client.post(self.url, self.body, HTTP_ACCEPT="application/json")
        self.assertEqual(response.json()["outcome"], "not_saved")
        lock.return_value.release.assert_called_once()
        self.assertFalse(WheelCloseReport.objects.exists())

    def test_csrf_is_required(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        self.assertEqual(client.post(self.url, self.body).status_code, 403)

    def test_evidence_is_append_only_and_never_executable(self):
        report = self.create_report()
        with self.assertRaises(ValidationError):
            report.save()
        with self.assertRaises(ValidationError):
            WheelCloseReport.objects.filter(pk=report.pk).update(symbol="MSFT")
        with self.assertRaises(ValidationError):
            WheelCloseReport.objects.create(family=self.family, symbol="TSLA", target_date=date(2026, 9, 2),
                request_key=uuid4(), evidence={**evidence(), "execution_allowed": True})
