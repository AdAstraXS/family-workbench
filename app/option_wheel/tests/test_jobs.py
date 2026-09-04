from datetime import timedelta
import json
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4
import subprocess

from django.contrib.auth import get_user_model
from django.core import signing
from django.db import IntegrityError, transaction
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from family_core.models import Family, FamilyMember
from ledger.models import BankAccount
from portfolio.models import InvestmentAccount, Security
from option_wheel.analysis_service import WheelAnalysisError, persist_probe_symbol
from option_wheel.jobs import enqueue, fetch_probe, job_payload, launch_job, run_job
from option_wheel.models import WheelAnalysisJob, WheelBrokerAccountSnapshot, WheelPolicy, WheelDecision, WheelMarketSnapshot, WheelOptionQuoteSnapshot
from option_wheel.tests.test_analysis_service import WheelAnalysisServiceTests
from option_wheel.templatetags.wheel_display import wheel_reason, wheel_reasons


class JobTests(TestCase):
    metadata = WheelAnalysisServiceTests.metadata
    probe_result = WheelAnalysisServiceTests.probe_result

    def setUp(self):
        self.family = Family.objects.create(name="Job family")
        self.user = get_user_model().objects.create_user(username="job-admin", is_superuser=True, is_staff=True)
        member = FamilyMember.objects.create(family=self.family, user=self.user, display_name="我")
        bank = BankAccount.objects.create(family=self.family, member=member, account_name="盈透证券", supports_investment=True)
        self.account = InvestmentAccount.objects.create(bank_account=bank)
        self.stock = Security.objects.create(symbol="TSLA", name="Tesla", market="US", currency="USD", asset_type=Security.TYPE_STOCK)
        self.policy = WheelPolicy.objects.create(family=self.family, account=self.account, underlying=self.stock)
        self.snapshot = WheelBrokerAccountSnapshot.objects.create(
            family=self.family, account=self.account, source_kind="manual_file", source_reference="fixture",
            currency="USD", settled_cash=120000, unsettled_cash=0, reserved_cash=0, nav=120000,
            margin_loan_balance=0, uses_margin=False, positions_summary={}, open_obligations={},
            source_as_of=timezone.now(), data_status="complete",
        )
        self.selection = {"account_ids": [self.account.pk], "symbols": ["TSLA"]}
        self.client.force_login(self.user)

    def create_job(self, **kwargs):
        return WheelAnalysisJob.objects.create(family=self.family, requested_by=self.user, selection=self.selection,
            expires_at=timezone.now() + timedelta(minutes=4), **kwargs)

    def payload(self):
        return {**self.selection, "confirm_read_only": "yes", "request_token": signing.dumps(
            {"family": self.family.pk, "key": str(uuid4())}, salt="wheel-live-job-v1")}

    @patch("option_wheel.jobs.launch_job")
    @patch("option_wheel.jobs.fetch_probe")
    def test_short_post_and_both_duplicate_tokens_and_tabs(self, fetch, launch):
        payload = self.payload()
        with self.captureOnCommitCallbacks(execute=True):
            first = self.client.post(reverse("option_wheel:refresh_analysis"), payload, HTTP_ACCEPT="application/json")
            second = self.client.post(reverse("option_wheel:refresh_analysis"), payload, HTTP_ACCEPT="application/json")
            third = self.client.post(reverse("option_wheel:refresh_analysis"), self.payload(), HTTP_ACCEPT="application/json")
        self.assertEqual(first.status_code, 202)
        self.assertEqual(first.json()["id"], second.json()["id"])
        self.assertEqual(first.json()["id"], third.json()["id"])
        self.assertEqual(first.json()["status"], "queued")
        self.assertEqual(WheelAnalysisJob.objects.count(), 1)
        launch.assert_called_once()
        fetch.assert_not_called()

    @patch("option_wheel.jobs.fetch_probe")
    def test_real_atomic_save_and_worker_claim_once(self, fetch):
        job = self.create_job()
        fetch.return_value = [self.probe_result()]
        run_job(job.pk)
        run_job(job.pk)
        job.refresh_from_db()
        self.assertEqual(job.status, "saved")
        self.assertEqual(WheelDecision.objects.count(), 1)
        self.assertEqual(job.decision_ids, [WheelDecision.objects.get().pk])
        self.assertFalse(WheelDecision.objects.get().execution_gate_open)
        fetch.assert_called_once_with(["TSLA"], covered_call_symbols=set())
        data = self.client.get(reverse("option_wheel:job_status", args=[job.pk])).json()
        self.assertEqual(data["results"][0]["url"], reverse("option_wheel:decision_detail", args=job.decision_ids))

    @patch("option_wheel.jobs.fetch_probe")
    def test_partial_persistence_rolls_back_job_and_all_evidence(self, fetch):
        job = self.create_job()
        fetch.return_value = [self.probe_result()]
        def fail_after_write(**kwargs):
            persist_probe_symbol(**kwargs)
            raise WheelAnalysisError("模拟保存失败")
        with patch("option_wheel.jobs.persist_probe_symbol", side_effect=fail_after_write):
            run_job(job.pk)
        job.refresh_from_db()
        self.assertEqual(job.status, "failed")
        self.assertFalse(WheelDecision.objects.exists())
        self.assertFalse(WheelMarketSnapshot.objects.exists())

    @patch("option_wheel.jobs.fetch_probe")
    def test_capacity_and_policy_rechecked_after_query(self, fetch):
        job = self.create_job()
        def changed(*args, **kwargs):
            WheelPolicy.objects.filter(pk=self.policy.pk).update(enabled=False)
            return [self.probe_result()]
        fetch.side_effect = changed
        run_job(job.pk)
        job.refresh_from_db()
        self.assertEqual(job.status, "failed")
        self.assertFalse(WheelDecision.objects.exists())

    @patch("option_wheel.jobs.launch_job")
    def test_expired_job_replaced_only_on_post_and_old_worker_cannot_save(self, launch):
        old = self.create_job(status="running")
        WheelAnalysisJob.objects.filter(pk=old.pk).update(expires_at=timezone.now()-timedelta(seconds=1))
        response = self.client.get(reverse("option_wheel:job_status", args=[old.pk]))
        self.assertEqual(response.json()["status"], "interrupted")
        old.refresh_from_db()
        self.assertEqual(old.status, "running")  # GET never repairs or writes.
        new = enqueue(self.family, self.user, uuid4(), self.selection)
        old.refresh_from_db()
        self.assertEqual(old.status, "interrupted")
        self.assertNotEqual(old.pk, new.pk)
        with patch("option_wheel.jobs.fetch_probe") as fetch:
            run_job(old.pk)
            fetch.assert_not_called()

    def test_database_enforces_single_active_job(self):
        self.create_job()
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.create_job()

    @patch("option_wheel.jobs.launch_job")
    @patch("option_wheel.jobs.fetch_probe")
    def test_status_detail_and_index_get_are_read_only_and_family_scoped(self, fetch, launch):
        job = self.create_job()
        urls = [reverse("option_wheel:job_status", args=[job.pk]), reverse("option_wheel:job_detail", args=[job.pk]), reverse("option_wheel:index")]
        for url in urls:
            self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(self.client.get(urls[0])["Cache-Control"], "no-store")
        self.user.is_superuser = False
        self.user.save()
        self.assertEqual(self.client.get(urls[0]).status_code, 200)
        other = get_user_model().objects.create_user(username="other")
        FamilyMember.objects.create(family=Family.objects.create(name="other"), user=other, display_name="other")
        self.client.force_login(other)
        for url in urls[:2]:
            self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.post(urls[0]).status_code, 405)
        fetch.assert_not_called()
        launch.assert_not_called()

    def test_bad_or_cross_family_token_and_unready_account_rejected(self):
        url = reverse("option_wheel:refresh_analysis")
        for token in ("invalid", signing.dumps({"family": 99999, "key": str(uuid4())}, salt="wheel-live-job-v1")):
            self.assertEqual(self.client.post(url, {**self.payload(), "request_token": token}).status_code, 400)
        with patch("option_wheel.jobs.validate_selection", side_effect=WheelAnalysisError("容量已过期")):
            self.assertEqual(self.client.post(url, self.payload()).status_code, 400)
        self.assertFalse(WheelAnalysisJob.objects.exists())

    @patch("option_wheel.jobs.subprocess.Popen", side_effect=OSError("secret"))
    def test_launcher_failure_is_visible_without_provider_details(self, popen):
        job = self.create_job()
        launch_job(job.pk)
        job.refresh_from_db()
        self.assertEqual(job.status, "failed")
        self.assertNotIn("secret", job.message)

    @patch("option_wheel.jobs.subprocess.run")
    def test_probe_frames_fail_closed_and_timeout_has_no_raw_error(self, run):
        result = {"status": "success", "symbols": [{"symbol": "US.TSLA"}]}
        run.return_value = SimpleNamespace(returncode=0, stdout="log\nWHEEL_LIVE:" + json.dumps(result))
        self.assertEqual(fetch_probe(["TSLA"]), result["symbols"])
        command = run.call_args.args[0]
        self.assertNotIn("--calls-for=TSLA", command)
        self.assertEqual(fetch_probe(["TSLA"], {"TSLA"}), result["symbols"])
        self.assertIn("--calls-for=TSLA", run.call_args.args[0])
        result["symbols"] = []
        run.return_value.stdout = "WHEEL_LIVE:" + json.dumps(result)
        with self.assertRaises(WheelAnalysisError): fetch_probe(["TSLA"])
        run.return_value.stdout = "WHEEL_LIVE:" + json.dumps({"status": "partial", "errors": [{"source": "chain", "category": "provider_error", "error": "not logged secret=do-not-publish"}]})
        with self.assertRaisesMessage(WheelAnalysisError, "服务提示未登录") as error: fetch_probe(["TSLA"])
        self.assertNotIn("do-not-publish", str(error.exception))
        run.side_effect = subprocess.TimeoutExpired("secret", 180)
        with self.assertRaisesMessage(WheelAnalysisError, "订阅清理状态需核对"): fetch_probe(["TSLA"])

    def test_chinese_reason_and_model_probability_disclaimer(self):
        self.assertIn("券商端复核保证金", wheel_reason("cash_insufficient"))
        self.assertIn("备兑", wheel_reason("covered_shares_insufficient"))
        self.assertIn("报价", wheel_reasons(["quote_age_expired"]))
        response = self.client.get(reverse("option_wheel:index"))
        self.assertContains(response, "不等同于真实提前指派概率")
        self.assertContains(response, "采集时冻结")

    @patch("option_wheel.jobs.fetch_probe")
    def test_multiple_accounts_share_probe_but_save_separate_decisions(self, fetch):
        bank = BankAccount.objects.create(family=self.family, member=self.account.bank_account.member,
            account_name="致富证券（公户）", supports_investment=True)
        second = InvestmentAccount.objects.create(bank_account=bank)
        WheelPolicy.objects.create(family=self.family, account=second, underlying=self.stock)
        WheelBrokerAccountSnapshot.objects.create(
            family=self.family, account=second, source_kind="manual_file", source_reference="second",
            currency="USD", settled_cash=120000, unsettled_cash=0, reserved_cash=0, nav=120000,
            margin_loan_balance=0, uses_margin=False, positions_summary={}, open_obligations={},
            source_as_of=timezone.now(), data_status="complete",
        )
        self.selection["account_ids"].append(second.pk)
        job = self.create_job()
        fetch.return_value = [self.probe_result()]
        errors = []
        def persist(**kwargs):
            try:
                return persist_probe_symbol(**kwargs)
            except Exception as exc:
                errors.append(repr(exc))
                raise
        with patch("option_wheel.jobs.persist_probe_symbol", side_effect=persist):
            run_job(job.pk)
        job.refresh_from_db()
        self.assertEqual(job.status, "saved", job.message + str(errors))
        self.assertEqual(len(job.decision_ids), 2)
        self.assertEqual(WheelOptionQuoteSnapshot.objects.count(), 1)
        fetch.assert_called_once()

    @patch("option_wheel.jobs.fetch_probe")
    def test_superseded_worker_and_revoked_admin_never_commit(self, fetch):
        for condition in ("interrupted", "revoked"):
            job = self.create_job()
            def changed(*args, **kwargs):
                if condition == "interrupted":
                    WheelAnalysisJob.objects.filter(pk=job.pk).update(status="interrupted")
                else:
                    get_user_model().objects.filter(pk=self.user.pk).update(is_superuser=False)
                return [self.probe_result()]
            fetch.side_effect = changed
            run_job(job.pk)
            self.assertFalse(WheelDecision.objects.exists())


class JobConcurrencyTests(TransactionTestCase):
    serialized_rollback = True
    setUp = JobTests.setUp

    def _fixture_teardown(self):
        # TransactionTestCase flush removes migration-seeded dictionaries. Restore
        # them so the project's --keepdb workflow remains repeatable.
        from django.db import connections
        super()._fixture_teardown()
        for alias in self._databases_names(include_mirrors=False):
            connection = connections[alias]
            connection.creation.deserialize_db_from_string(connection._test_serialized_contents)

    @patch("option_wheel.jobs.launch_job")
    def test_two_connections_enqueue_only_one_job(self, launch):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from django.db import close_old_connections, connection
        if connection.vendor != "postgresql":
            self.skipTest("Production uses PostgreSQL row locks")
        barrier = Barrier(2)
        def submit():
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return enqueue(self.family, self.user, uuid4(), self.selection).pk
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            one, two = pool.submit(submit), pool.submit(submit)
            self.assertEqual(one.result(timeout=15), two.result(timeout=15))
        self.assertEqual(WheelAnalysisJob.objects.count(), 1)
        launch.assert_called_once()
