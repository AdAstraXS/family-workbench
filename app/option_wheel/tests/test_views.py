from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from family_core.models import Family, FamilyMember
from ledger.models import BankAccount
from portfolio.models import InvestmentAccount, Security

from option_wheel.account_capacity import CapacityImportError
from option_wheel.models import (
    DataStatus,
    DelayStatus,
    EventStatus,
    Freshness,
    OverallStatus,
    Strategy,
    TechnicalStatus,
    WheelBrokerAccountSnapshot,
    WheelCandidate,
    WheelDecision,
    WheelMarketSnapshot,
    WheelOptionQuoteSnapshot,
    WheelPolicy,
)


class OptionWheelPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.family = Family.objects.create(name="Wheel Page Family")
        cls.user = get_user_model().objects.create_user(username="wheel-page")
        cls.member = FamilyMember.objects.create(
            family=cls.family,
            user=cls.user,
            display_name="Wheel Member",
        )
        cls.tsla = Security.objects.create(
            symbol="TSLA",
            name="Tesla",
            market="US",
            asset_type=Security.TYPE_STOCK,
            currency="USD",
        )

    @classmethod
    def make_account(cls, family, member, name):
        bank = BankAccount.objects.create(
            family=family,
            member=member,
            account_name=name,
            supports_investment=True,
        )
        return InvestmentAccount.objects.create(bank_account=bank)

    def setUp(self):
        self.client.force_login(self.user)

    def create_policy(self, account, underlying=None):
        return WheelPolicy.objects.create(
            family=account.family,
            account=account,
            underlying=underlying or self.tsla,
        )

    def create_complete_account_snapshot(self, account, *, source_as_of=None):
        return WheelBrokerAccountSnapshot.objects.create(
            family=account.family,
            account=account,
            source_kind=WheelBrokerAccountSnapshot.SOURCE_MANUAL_FILE,
            source_reference=f"fixture-{account.pk}",
            currency="USD",
            settled_cash=Decimal("120000"),
            unsettled_cash=Decimal("0"),
            nav=Decimal("120000"),
            reserved_cash=Decimal("0"),
            margin_loan_balance=Decimal("0"),
            uses_margin=False,
            positions_summary={},
            open_obligations={},
            source_as_of=source_as_of or timezone.now(),
            data_status=DataStatus.COMPLETE,
        )

    def create_market(self, underlying=None):
        return WheelMarketSnapshot.objects.create(
            underlying=underlying or self.tsla,
            provider="Futu",
            provider_symbol="US.TSLA",
            last_price=Decimal("341.25"),
            source_as_of=timezone.now(),
            market_session="regular",
            regular_session_verified=True,
            calendar_reference="US-2026",
            delay_status=DelayStatus.REAL_TIME,
            freshness_status=Freshness.FRESH,
            data_quality=DataStatus.COMPLETE,
        )

    def create_decision(self, policy, snapshot, market):
        return WheelDecision.objects.create(
            family=policy.family,
            account=policy.account,
            underlying=policy.underlying,
            policy=policy,
            account_snapshot=snapshot,
            market_snapshot=market,
            input_fingerprint=f"decision-{policy.pk}",
            event_status=EventStatus.CLEAR,
            technical_status=TechnicalStatus.COMPLETE,
            overall_status=OverallStatus.INVESTIGATION,
            blockers=["执行总闸门关闭"],
            frozen_input={
                "earnings_status": "未来七天无已核验财报",
                "earnings_as_of": "2026-08-30T09:00:00+08:00",
            },
        )

    def test_login_is_required(self):
        self.client.logout()

        response = self.client.get(reverse("option_wheel:index"))

        self.assertRedirects(
            response,
            f"{reverse('login')}?next={reverse('option_wheel:index')}",
        )

    def test_duplicate_broker_names_fail_closed_until_one_has_capacity_evidence(self):
        first = self.make_account(self.family, self.member, "盈透证券")
        self.make_account(self.family, self.member, "盈透证券")

        response = self.client.get(reverse("option_wheel:index"))

        self.assertContains(response, "身份有重名")
        self.assertContains(response, "需先生成对应账户的容量快照")

        self.create_complete_account_snapshot(first)
        response = self.client.get(reverse("option_wheel:index"))

        self.assertNotContains(response, "身份有重名")
        self.assertContains(response, "120000.00 USD")

    def test_active_family_member_sees_read_only_empty_state_and_navigation(self):
        response = self.client.get(reverse("option_wheel:index"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "仅分析，不下单")
        self.assertContains(response, "数据是否就绪")
        self.assertContains(response, "尚未就绪")
        self.assertContains(response, "致富证券（公户）")
        self.assertContains(response, "盈透证券")
        self.assertContains(response, "TSLA")
        self.assertContains(response, "未来财报")
        self.assertContains(response, "尚无独立持久化证据", count=2)
        self.assertContains(
            response,
            '<a href="/option-wheel/">期权车轮</a>',
            html=True,
        )

    def test_unlinked_non_superuser_is_forbidden(self):
        user = get_user_model().objects.create_user(username="wheel-outsider")
        self.client.force_login(user)

        response = self.client.get(reverse("option_wheel:index"))

        self.assertEqual(response.status_code, 403)

    def test_other_family_records_are_not_rendered(self):
        other_family = Family.objects.create(name="Secret Other Family")
        other_member = FamilyMember.objects.create(
            family=other_family,
            display_name="Other Member",
        )
        other_account = self.make_account(
            other_family,
            other_member,
            "其他家庭秘密账户",
        )
        self.create_policy(other_account)

        response = self.client.get(reverse("option_wheel:index"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "其他家庭秘密账户")
        self.assertNotContains(response, "Secret Other Family")

    def test_get_does_not_write_wheel_tables(self):
        models = (
            WheelPolicy,
            WheelBrokerAccountSnapshot,
            WheelMarketSnapshot,
            WheelOptionQuoteSnapshot,
            WheelDecision,
            WheelCandidate,
        )
        before = [model.objects.count() for model in models]

        response = self.client.get(reverse("option_wheel:index"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(before, [model.objects.count() for model in models])

    @patch("option_wheel.views.build_portfolio_capacity")
    def test_get_does_not_calculate_capacity(self, build_capacity):
        self.make_account(self.family, self.member, "盈透证券")

        response = self.client.get(reverse("option_wheel:index"))

        self.assertEqual(response.status_code, 200)
        build_capacity.assert_not_called()

    def test_account_owned_by_me_is_preferred_over_same_name(self):
        self.make_account(self.family, self.member, "盈透证券")
        owner = FamilyMember.objects.create(family=self.family, display_name="我")
        preferred = self.make_account(self.family, owner, "盈透证券")

        response = self.client.get(reverse("option_wheel:index"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "身份有重名")
        card = next(card for card in response.context["account_cards"] if card["name"] == "盈透证券")
        self.assertEqual(card["account"], preferred)
        self.assertContains(response, "投资组合账户：盈透证券 · 我")

    @patch("option_wheel.views.build_portfolio_capacity")
    def test_capacity_preview_is_rendered_without_writing_snapshot(self, build_capacity):
        account = self.make_account(self.family, self.member, "盈透证券")
        build_capacity.return_value = SimpleNamespace(
            settled_cash=Decimal("52000.25"),
            unsettled_cash=Decimal("100.00"),
            reserved_cash=Decimal("32000.00"),
            nav=Decimal("121000.00"),
            source_as_of=timezone.now(),
            positions_summary={"count": 3},
            open_obligations={"count": 1},
        )

        response = self.client.post(
            reverse("option_wheel:index"),
            {
                "action": "preview_capacity",
                "account_id": account.pk,
                "confirm_no_margin": "yes",
                "confirm_no_open_orders": "yes",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "预演结果（未保存）")
        self.assertContains(response, "20000.25 USD")
        self.assertContains(response, "3 项")
        self.assertNotContains(response, "保存为正式容量快照")
        self.assertEqual(WheelBrokerAccountSnapshot.objects.count(), 0)
        build_capacity.assert_called_once_with(
            account_id=account.pk,
            confirm_no_margin=True,
            confirm_no_open_orders=True,
        )

    @patch("option_wheel.views.build_portfolio_capacity")
    def test_capacity_preview_shows_fail_closed_error(self, build_capacity):
        account = self.make_account(self.family, self.member, "盈透证券")
        build_capacity.side_effect = CapacityImportError("必须明确确认该账户当前没有融资或借贷。")

        response = self.client.post(
            reverse("option_wheel:index"),
            {"action": "preview_capacity", "account_id": account.pk},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "无法完成预演")
        self.assertContains(response, "必须明确确认该账户当前没有融资或借贷。")
        self.assertEqual(WheelBrokerAccountSnapshot.objects.count(), 0)

    @patch("option_wheel.views.build_portfolio_capacity")
    def test_capacity_preview_rejects_other_family_account(self, build_capacity):
        other_family = Family.objects.create(name="Other Family")
        other_member = FamilyMember.objects.create(
            family=other_family,
            display_name="Other Member",
        )
        other_account = self.make_account(other_family, other_member, "盈透证券")

        response = self.client.post(
            reverse("option_wheel:index"),
            {
                "action": "preview_capacity",
                "account_id": other_account.pk,
                "confirm_no_margin": "yes",
                "confirm_no_open_orders": "yes",
            },
        )

        self.assertEqual(response.status_code, 403)
        build_capacity.assert_not_called()

    @patch("option_wheel.views.import_portfolio_capacity")
    @patch("option_wheel.views.build_portfolio_capacity")
    def test_non_superuser_cannot_save_capacity_snapshot(
        self,
        build_capacity,
        import_capacity,
    ):
        account = self.make_account(self.family, self.member, "盈透证券")

        response = self.client.post(
            reverse("option_wheel:index"),
            {
                "action": "save_capacity",
                "account_id": account.pk,
                "confirm_no_margin": "yes",
                "confirm_no_open_orders": "yes",
                "confirm_save_snapshot": "yes",
            },
        )

        self.assertEqual(response.status_code, 403)
        build_capacity.assert_not_called()
        import_capacity.assert_not_called()

    @patch("option_wheel.views.import_portfolio_capacity")
    @patch("option_wheel.views.build_portfolio_capacity")
    def test_superuser_must_confirm_before_saving_capacity_snapshot(
        self,
        build_capacity,
        import_capacity,
    ):
        account = self.make_account(self.family, self.member, "盈透证券")
        build_capacity.return_value = SimpleNamespace(
            settled_cash=Decimal("52000.25"),
            unsettled_cash=Decimal("100.00"),
            reserved_cash=Decimal("32000.00"),
            nav=Decimal("121000.00"),
            source_as_of=timezone.now(),
            positions_summary={"count": 3},
            open_obligations={"count": 1},
        )
        admin = get_user_model().objects.create_superuser(
            username="wheel-admin",
            email="wheel-admin@example.com",
            password="unused",
        )
        self.client.force_login(admin)

        response = self.client.post(
            reverse("option_wheel:index"),
            {
                "action": "save_capacity",
                "account_id": account.pk,
                "confirm_no_margin": "yes",
                "confirm_no_open_orders": "yes",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "保存前必须再次确认")
        self.assertContains(response, "保存为正式容量快照")
        import_capacity.assert_not_called()

    @patch("option_wheel.views.import_portfolio_capacity")
    @patch("option_wheel.views.build_portfolio_capacity")
    def test_superuser_can_save_recomputed_capacity_snapshot(
        self,
        build_capacity,
        import_capacity,
    ):
        account = self.make_account(self.family, self.member, "盈透证券")
        evidence = SimpleNamespace(
            settled_cash=Decimal("52000.25"),
            unsettled_cash=Decimal("100.00"),
            reserved_cash=Decimal("32000.00"),
            nav=Decimal("121000.00"),
            source_as_of=timezone.now(),
            positions_summary={"count": 3},
            open_obligations={"count": 1},
        )
        build_capacity.return_value = evidence
        import_capacity.return_value = SimpleNamespace(snapshot_created=True)
        admin = get_user_model().objects.create_superuser(
            username="wheel-save-admin",
            email="wheel-save-admin@example.com",
            password="unused",
        )
        self.client.force_login(admin)

        response = self.client.post(
            reverse("option_wheel:index"),
            {
                "action": "save_capacity",
                "account_id": account.pk,
                "confirm_no_margin": "yes",
                "confirm_no_open_orders": "yes",
                "confirm_save_snapshot": "yes",
            },
            follow=True,
        )

        self.assertRedirects(response, reverse("option_wheel:index"))
        self.assertContains(response, "已保存 盈透证券 的正式容量快照")
        build_capacity.assert_called_once_with(
            account_id=account.pk,
            confirm_no_margin=True,
            confirm_no_open_orders=True,
        )
        import_capacity.assert_called_once_with(evidence=evidence, commit=True)

    def test_account_market_event_and_candidate_evidence_are_rendered(self):
        zhifu = self.make_account(self.family, self.member, "致富证券（公户）")
        ibkr = self.make_account(self.family, self.member, "盈透证券")
        policy = self.create_policy(zhifu)
        zhifu_snapshot = self.create_complete_account_snapshot(zhifu)
        self.create_complete_account_snapshot(ibkr)
        market = self.create_market()
        decision = self.create_decision(policy, zhifu_snapshot, market)
        quote = WheelOptionQuoteSnapshot.objects.create(
            underlying=self.tsla,
            market_snapshot=market,
            provider="Futu",
            provider_contract_code="TSLA260904P00320000",
            currency="USD",
            option_type=WheelOptionQuoteSnapshot.PUT,
            expiration=date.today() + timedelta(days=5),
            strike=Decimal("320"),
            assignment_probability=Decimal("12.5"),
            quote_as_of=timezone.now(),
        )
        WheelCandidate.objects.create(
            decision=decision,
            option_quote=quote,
            candidate_key="tsla-put-320",
            strategy=Strategy.SELL_PUT,
            status=OverallStatus.BLOCKED,
            required_cash=Decimal("32000"),
            premium_total=Decimal("300"),
            break_even=Decimal("317"),
            annualized_premium_rate=Decimal("0.1425"),
            assignment_probability=Decimal("12.5"),
            premium_preference_match=True,
            dte_preference_match=True,
            exclusion_reasons=["财报证据仍需人工核对"],
        )

        response = self.client.get(reverse("option_wheel:index"))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["data_ready"])
        self.assertContains(response, "341.25")
        self.assertContains(response, "实时")
        self.assertContains(response, "未来七天无已核验财报")
        self.assertContains(response, "12.50%")
        self.assertContains(response, "14.25%")
        self.assertContains(response, "财报证据仍需人工核对")
        self.assertContains(response, "32000.00 USD")
        self.assertContains(response, "仅分析，不下单")

    def test_stale_account_snapshot_keeps_data_not_ready(self):
        zhifu = self.make_account(self.family, self.member, "致富证券（公户）")
        ibkr = self.make_account(self.family, self.member, "盈透证券")
        policy = self.create_policy(zhifu)
        zhifu_snapshot = self.create_complete_account_snapshot(zhifu)
        self.create_complete_account_snapshot(
            ibkr,
            source_as_of=timezone.now() - timedelta(days=2),
        )
        market = self.create_market()
        self.create_decision(policy, zhifu_snapshot, market)

        response = self.client.get(reverse("option_wheel:index"))

        self.assertFalse(response.context["data_ready"])
        self.assertContains(response, "盈透证券容量证据未就绪")
