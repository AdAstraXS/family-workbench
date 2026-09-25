"""Regression tests for the September project review, using synthetic data only."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from threading import Barrier
from unittest.mock import patch

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.db import close_old_connections, connection, connections, transaction
from django.test import RequestFactory, TestCase, TransactionTestCase, skipUnlessDBFeature
from django.test.utils import CaptureQueriesContext
from django.urls import resolve, reverse
from django.utils import timezone

from family_core.context_processors import page_navigation
from family_core.models import ExchangeRate, Family, FamilyMember
from family_core.navigation import return_url
from ledger.models import (
    AssetBalanceEntry, AssetBalanceSnapshot, BankAccount, IncomeRecord,
    InvestmentGoalActualOverride, InvestmentGoalPlan, InvestmentGoalPoint, InvestmentGoalSetting,
)
from ledger.valuation import MissingCashflowRate, cashflow_amount
from .models import (
    DailyExchangeRateFetch, InvestmentAccount, InvestmentCashMovement,
    InvestmentPosition, InvestmentTransaction, OptionContract, Security, SecurityQuoteConfig,
)
from .services import rebuild_position, settle_option_position


def fixtures(test):
    test.family = Family.objects.create(name="Synthetic household")
    test.user = get_user_model().objects.create_user(username="hardening-member")
    test.member = FamilyMember.objects.create(family=test.family, user=test.user, display_name="Member")
    test.bank = BankAccount.objects.create(family=test.family, member=test.member,
        account_name="Synthetic broker", supports_investment=True)
    test.account = InvestmentAccount.objects.create(bank_account=test.bank)
    test.security = Security.objects.create(symbol="SYN", name="Synthetic security", market="US", currency="USD")


def buy(test, quantity=100):
    return InvestmentTransaction.objects.create(account=test.account, security=test.security,
        trade_date=date(2026, 9, 1), trade_type="buy", status="completed",
        quantity=quantity, price=1, amount=quantity, currency="USD")


class FinancialHardeningTests(TestCase):
    def setUp(self):
        fixtures(self)
        self.client.force_login(self.user)

    def snapshot_data(self, **overrides):
        return {"family": self.family.pk, "snapshot_date": "2026-09-01", "base_currency": "CNY",
            "usd_to_base": "0", "hkd_to_base": "0", "entries-TOTAL_FORMS": "1",
            "entries-INITIAL_FORMS": "0", "entries-MIN_NUM_FORMS": "0", "entries-MAX_NUM_FORMS": "1000",
            "entries-0-member": self.member.pk, "entries-0-account": self.bank.pk,
            "entries-0-currency": "USD", "entries-0-original_amount": "100", "entries-0-display_order": "1",
            **overrides}

    def test_missing_snapshot_rate_blocks_publish_but_allows_draft(self):
        response = self.client.post(reverse("ledger:asset_snapshot_create"), self.snapshot_data())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "请填写大于 0 的对应汇率")
        self.assertFalse(AssetBalanceSnapshot.objects.exists())
        response = self.client.post(reverse("ledger:asset_snapshot_create"), self.snapshot_data(save_action="draft"))
        self.assertEqual(response.status_code, 302)
        snapshot = AssetBalanceSnapshot.objects.get()
        self.assertTrue(snapshot.is_draft)
        self.assertEqual(snapshot.entries.get().base_amount, 0)
        entry = snapshot.entries.get()
        data = self.snapshot_data(**{"entries-INITIAL_FORMS": "1", "entries-0-id": entry.pk})
        response = self.client.post(reverse("ledger:asset_snapshot_edit", args=[snapshot.pk]), data)
        self.assertEqual(response.status_code, 200)
        snapshot.refresh_from_db()
        self.assertTrue(snapshot.is_draft)
        data["usd_to_base"] = "7"
        response = self.client.post(reverse("ledger:asset_snapshot_edit", args=[snapshot.pk]), data)
        self.assertEqual(response.status_code, 302)
        snapshot.refresh_from_db()
        self.assertFalse(snapshot.is_draft)
        self.assertEqual(snapshot.entries.get().base_amount, 700)

    def test_snapshot_detail_failure_rolls_back_header(self):
        with patch("ledger.views.save_asset_snapshot_formset", side_effect=RuntimeError("injected failure")):
            with self.assertRaisesRegex(RuntimeError, "injected failure"):
                self.client.post(reverse("ledger:asset_snapshot_create"), self.snapshot_data(usd_to_base="7"))
        self.assertFalse(AssetBalanceSnapshot.objects.exists())

    def test_budget_detail_failure_rolls_back_header(self):
        from ledger.models import AnnualBudget
        data = {"family": self.family.pk, "year": "2027", "lines-TOTAL_FORMS": "0",
            "lines-INITIAL_FORMS": "0", "lines-MIN_NUM_FORMS": "0", "lines-MAX_NUM_FORMS": "1000"}
        with patch("ledger.views.save_budget_formset", side_effect=RuntimeError("budget detail failure")):
            with self.assertRaisesRegex(RuntimeError, "budget detail failure"):
                self.client.post(reverse("ledger:annual_budget_create"), data)
        self.assertFalse(AnnualBudget.objects.exists())

    def test_missing_rate_keeps_original_cashflow_rows_readable(self):
        from ledger.models import ExpenseRecord
        expense = ExpenseRecord.objects.create(family=self.family, member=self.member,
            expense_date=date(2026, 9, 1), currency="USD", amount=123)
        response = self.client.get(reverse("ledger:expense_year_detail", args=[2026]))
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["expense_family_total"])
        self.assertEqual(response.context["expense_rows"][0]["record"].pk, expense.pk)
        self.assertContains(response, "缺少当日或此前可用的人民币汇率")

    def test_script_boundary_is_escaped_and_json_remains_parseable(self):
        import json
        marker = '</script><script>/*INERT_MARKER*/</script>'
        self.bank.account_name = marker
        self.bank.save()
        response = self.client.get(reverse("ledger:asset_snapshot_create"))
        body = response.content.decode()
        self.assertNotIn(marker, body)
        data = body.split('<script id="account-options" type="application/json">', 1)[1].split('</script>', 1)[0]
        self.assertIn(marker, json.loads(data)[0]["label"])

    def test_home_converts_mixed_cash_and_excludes_draft(self):
        for currency in ("USD", "CNY"):
            InvestmentCashMovement.objects.create(account=self.account, movement_date=timezone.localdate(),
                movement_type="deposit", currency=currency, amount=100)
        ExchangeRate.objects.create(base_currency="USD", quote_currency="CNY", rate=7, rate_date=timezone.localdate())
        snapshot = AssetBalanceSnapshot.objects.create(family=self.family, snapshot_date=timezone.localdate(), is_draft=True)
        AssetBalanceEntry.objects.create(snapshot=snapshot, member=self.member, base_amount=999)
        response = self.client.get(reverse("dashboard:home"))
        self.assertEqual(response.context["total_investment_asset"], 800)
        self.assertIsNone(response.context["latest_snapshot"])

    def test_cashflow_conversion_uses_occurrence_or_period_end(self):
        for day, rate in [(1, 7), (30, 8)]:
            ExchangeRate.objects.create(base_currency="USD", quote_currency="CNY", rate=rate, rate_date=date(2026, 9, day))
        record = IncomeRecord.objects.create(family=self.family, member=self.member,
            income_date=date(2026, 9, 2), amount=100, currency="USD")
        self.assertEqual(cashflow_amount(record), 700)
        record.period_start, record.period_end = date(2026, 9, 1), date(2026, 9, 30)
        record.save()
        self.assertEqual(cashflow_amount(record), 800)
        from ledger.views import build_cashflow_monthly_rows
        _, sections = build_cashflow_monthly_rows(2026)
        self.assertEqual(sections[0]["total"]["income_total"], 800)
        record.currency = "HKD"
        record.save()
        with self.assertRaises(MissingCashflowRate):
            cashflow_amount(record)
        response = self.client.get(reverse("ledger:overview"))
        self.assertContains(response, "收支汇总暂不完整")

    def test_goal_override_first_save_and_update(self):
        start = AssetBalanceSnapshot.objects.create(family=self.family, snapshot_date=date(2025, 12, 31))
        plan = InvestmentGoalPlan.objects.create(family=self.family, start_snapshot=start)
        setting = InvestmentGoalSetting.objects.create(plan=plan, member=self.member, initial_amount=100)
        InvestmentGoalPoint.objects.create(setting=setting, period_index=1, target_date=date(2026, 6, 30), target_amount=100)
        for amount in (123, 456):
            response = self.client.post(reverse("ledger:investment_goal_actual_override"),
                {"member": self.member.pk, "target_date": "2026-06-30", "amount": amount, "remark": "correction"})
            self.assertEqual(response.status_code, 302)
            self.assertEqual(InvestmentGoalActualOverride.objects.get().amount, amount)

    def test_get_account_pages_never_fetch_or_create_fetch_log(self):
        from family_core.models import SiteSetting
        SiteSetting.objects.all().delete()
        with patch("portfolio.exchange_rate_service.urlopen") as network, CaptureQueriesContext(connection) as queries:
            for url in [reverse("portfolio:account_list"), self.account.get_absolute_url()]:
                self.assertEqual(self.client.get(url).status_code, 200)
        network.assert_not_called()
        self.assertFalse(DailyExchangeRateFetch.objects.exists())
        writes = [q["sql"] for q in queries.captured_queries if q["sql"].lstrip().upper().startswith(("INSERT ", "UPDATE ", "DELETE "))]
        self.assertEqual(writes, [])
        self.assertFalse(SiteSetting.objects.exists())

    def test_assignment_legs_cannot_be_individually_changed_or_deleted(self):
        option = Security.objects.create(symbol="SYN-PUT", name="Synthetic put", market="US", currency="USD", asset_type="option")
        OptionContract.objects.create(security=option, underlying=self.security, option_type="put",
            strike_price=10, expiration_date=date(2026, 9, 18), multiplier=100)
        InvestmentTransaction.objects.create(account=self.account, security=option, trade_date=date(2026, 9, 1),
            trade_type="sell", position_effect="open", quantity=1, price=1, amount=100, currency="USD", status="completed")
        position = rebuild_position(self.account, option)
        close, underlying = settle_option_position(position, action="assignment", action_date=date(2026, 9, 18), quantity=Decimal(1), user=self.user)
        from .admin import InvestmentTransactionAdmin
        model_admin = InvestmentTransactionAdmin(InvestmentTransaction, admin.site)
        for item in [close, underlying]:
            self.assertEqual(self.client.post(reverse("portfolio:transaction_delete", args=[item.pk])).status_code, 302)
            self.assertEqual(self.client.post(reverse("portfolio:transaction_edit", args=[item.pk]), {}).status_code, 302)
            self.assertTrue(InvestmentTransaction.objects.filter(pk=item.pk).exists())
            with self.assertRaises(PermissionDenied):
                model_admin.delete_queryset(RequestFactory().post("/admin/"), InvestmentTransaction.objects.filter(pk=item.pk))
        position.refresh_from_db()
        self.assertEqual(position.quantity, 0)
        self.assertEqual(InvestmentPosition.objects.get(account=self.account, security=self.security).quantity, 100)

    def test_ipo_bulk_delete_rebuilds_positions_and_cash(self):
        from ipo.admin import HkIpoSubscriptionTradeAdmin
        from ipo.models import HkIpoListing, HkIpoSubscriptionTrade
        from .ipo_sync import sync_ipo_trade
        listing = HkIpoListing.objects.create(stock_code="99991", company_name="Synthetic IPO", stock_name="Synthetic",
            final_price=10, lot_size=100, listing_date=date(2026, 9, 1), allotment_result_date=date(2026, 8, 31))
        trade = HkIpoSubscriptionTrade.objects.create(listing=listing, member=self.member, account=self.bank, allotted_lots=1)
        sync_ipo_trade(trade.pk)
        item = InvestmentTransaction.objects.get(ipo_subscription_trade=trade)
        HkIpoSubscriptionTradeAdmin(HkIpoSubscriptionTrade, admin.site).delete_queryset(
            RequestFactory().post("/admin/"), HkIpoSubscriptionTrade.objects.filter(pk=trade.pk))
        self.assertFalse(InvestmentTransaction.objects.filter(pk=item.pk).exists())
        self.assertFalse(InvestmentCashMovement.objects.filter(transaction_id=item.pk).exists())
        self.assertEqual(InvestmentPosition.objects.get(account=self.account, security=item.security).quantity, 0)

    def test_pagination_reaches_oldest_transaction(self):
        first = buy(self, 1)
        for _ in range(100):
            buy(self, 1)
        response = self.client.get(reverse("portfolio:transaction_list"), {"page": "2"})
        self.assertEqual([item.pk for item in response.context["transactions"]], [first.pk])

    def test_fx_queries_do_not_grow_with_holdings(self):
        from .views import _account_dashboard_data
        ExchangeRate.objects.create(base_currency="USD", quote_currency="CNY", rate=7, rate_date=timezone.localdate())
        for number in range(20):
            security = Security.objects.create(symbol=f"SYN{number}", name="Synthetic", market="US", currency="USD")
            SecurityQuoteConfig.objects.create(security=security, provider="manual", max_age_hours=720)
            InvestmentPosition.objects.create(account=self.account, security=security, quantity=1,
                avg_cost=1, current_price=1, current_price_as_of=timezone.now(), position_date=timezone.localdate())
        request = RequestFactory().get("/portfolio/accounts/")
        request.user = self.user
        with CaptureQueriesContext(connection) as queries:
            _account_dashboard_data(request)
        fx_queries = [q for q in queries.captured_queries if 'FROM "family_core_exchangerate"' in q["sql"]]
        self.assertLessEqual(len(fx_queries), 2)

    def test_parent_navigation_and_untrusted_return_targets(self):
        for name, args, parent in [("investment_research:edit", [1], "/research/1/"),
                                  ("option_wheel:underlying_detail", ["SYN"], "/option-wheel/")]:
            request = RequestFactory().get(reverse(name, args=args))
            request.resolver_match = resolve(request.path)
            request.user = self.user
            self.assertEqual(page_navigation(request)["page_parent_url"], parent)
        for target in ["https://evil.example/", "//evil.example/", "/\\evil.example/", "/admin/"]:
            request = RequestFactory().get("/ledger/", {"return_to": target})
            self.assertIsNone(return_url(request))
        target = reverse("portfolio:transaction_list") + "?page=2"
        item = buy(self, 1)
        response = self.client.post(reverse("portfolio:transaction_delete", args=[item.pk]), {"return_to": target})
        self.assertRedirects(response, target, fetch_redirect_response=False)


@skipUnlessDBFeature("has_select_for_update")
class ConcurrentPositionTests(TransactionTestCase):
    serialized_rollback = True

    def _fixture_teardown(self):
        # Preserve migration-seeded dictionaries for the project's --keepdb runs.
        super()._fixture_teardown()
        for alias in self._databases_names(include_mirrors=False):
            db = connections[alias]
            db.creation.deserialize_db_from_string(db._test_serialized_contents)

    def setUp(self):
        fixtures(self)
        buy(self)
        rebuild_position(self.account, self.security)

    def test_concurrent_buys_preserve_facts_and_projection(self):
        gate = Barrier(2)
        def worker():
            close_old_connections()
            try:
                gate.wait(timeout=15)
                with transaction.atomic():
                    buy(self, 1)
                    rebuild_position(self.account, self.security)
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            one, two = pool.submit(worker), pool.submit(worker)
            one.result(timeout=30)
            two.result(timeout=30)
        position = InvestmentPosition.objects.get(account=self.account, security=self.security)
        self.assertEqual(position.quantity, 102)
        self.assertEqual(sum(InvestmentTransaction.objects.filter(account=self.account).values_list("quantity", flat=True)), 102)

    def test_opposite_account_moves_lock_in_consistent_order(self):
        bank = BankAccount.objects.create(family=self.family, member=self.member,
            account_name="Second synthetic broker", supports_investment=True)
        other = InvestmentAccount.objects.create(bank_account=bank)
        first = InvestmentTransaction.objects.get(account=self.account)
        second = InvestmentTransaction.objects.create(account=other, security=self.security,
            trade_date=date(2026, 9, 1), trade_type="buy", status="completed",
            quantity=50, price=1, amount=50, currency="USD")
        rebuild_position(other, self.security)
        gate = Barrier(2)
        def move(pk, target):
            close_old_connections()
            try:
                item = InvestmentTransaction.objects.get(pk=pk)
                gate.wait(timeout=15)
                with transaction.atomic():
                    item.account = target
                    item.save()
                    for account in sorted([self.account, other], key=lambda obj: obj.pk):
                        rebuild_position(account, self.security)
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            one, two = pool.submit(move, first.pk, other), pool.submit(move, second.pk, self.account)
            one.result(timeout=30)
            two.result(timeout=30)
        self.assertEqual(InvestmentPosition.objects.get(account=self.account, security=self.security).quantity, 50)
        self.assertEqual(InvestmentPosition.objects.get(account=other, security=self.security).quantity, 100)
