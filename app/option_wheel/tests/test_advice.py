from copy import deepcopy
from decimal import Decimal
import json
from types import SimpleNamespace as Obj
from unittest.mock import patch, MagicMock
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from family_core.models import Family, FamilyMember
from ledger.models import BankAccount
from portfolio.models import InvestmentAccount, Security
from option_wheel.advice import build_advice_packet, validate_advice_result, SCHEMA
from option_wheel.analysis_service import persist_probe_symbol
from option_wheel.models import WheelPolicy, WheelBrokerAccountSnapshot, WheelDecision
from option_wheel.tests import test_analysis_service as fixtures


class AdviceContractTests(SimpleTestCase):
    def setUp(self):
        now = timezone.now()
        self.decision = Obj(underlying=Obj(symbol="INTC"), decision_time=now,
            ruleset_version="m1-v1", blockers=[], technical_snapshot=None, event_snapshot=None,
            market_snapshot=Obj(provider="futu", last_price=Decimal("30"), source_as_of=now),
            frozen_input={"secret": "DO_NOT_SEND"}, account=Obj(account_name="DO_NOT_SEND"))
        quote = Obj(provider="futu", quote_as_of=now, expiration=now.date(), strike=Decimal("29"),
            bid=Decimal("0.41"), ask=Decimal("0.44"), delta=Decimal("-0.2"),
            implied_volatility=Decimal("40"), volume=500, open_interest=1000, contract_multiplier=100)
        self.candidate = Obj(candidate_key="US.INTC_TEST_P", status="investigation",
            exclusion_reasons=["execution_gate_closed"], assignment_probability=Decimal("18.5"),
            warning_reasons=[], premium_preference_match=True, dte_preference_match=True,
            premium_total=Decimal("41"), option_quote=quote, strategy="sell_put")

    def packet(self, candidates=None):
        return build_advice_packet(self.decision, [self.candidate] if candidates is None else candidates)["packet"]

    def result(self, packet):
        return {"schema": SCHEMA, "input_hash": packet["input_hash"], "outcome": "compare",
            "summary": "仅比较冻结样本", "comparisons": [{"candidate_id": "C1", "reason": "样本理由", "caution": "存在风险"}],
            "limitations": ["新闻未覆盖"]}

    def test_allowlist_decimal_and_stable_hash(self):
        packet = self.packet()
        self.assertNotIn("DO_NOT_SEND", json.dumps(packet))
        self.assertNotIn("account", json.dumps(packet))
        self.assertEqual(packet["candidates"][0]["premium_per_contract"], "41.00")
        self.assertEqual(packet["input_hash"], self.packet()["input_hash"])
        self.assertFalse(packet["execution_allowed"])
        self.assertEqual(packet["news_coverage"], "not_provided")

    def test_blocked_candidate_not_rehabilitated(self):
        self.candidate.exclusion_reasons.append("cash_insufficient")
        self.assertEqual(self.packet()["candidates"], [])
        self.candidate.exclusion_reasons = []
        self.decision.blockers = ["event unknown"]
        self.assertEqual(self.packet()["candidates"], [])

    def test_missing_probability_is_compared_with_warning_but_bid_is_required(self):
        self.candidate.assignment_probability = None
        self.candidate.warning_reasons = ["quote_probability_missing"]
        self.assertEqual(len(self.packet()["candidates"]), 1)
        self.candidate.assignment_probability = Decimal("18")
        self.candidate.option_quote.bid = None
        self.assertEqual(self.packet()["candidates"], [])

    def test_low_probability_order_and_three_sample_limit(self):
        rows = [deepcopy(self.candidate) for i in range(4)]
        for i, row in enumerate(rows):
            row.candidate_key = "INTC" + str(i)
            row.assignment_probability = Decimal(4-i)
        self.assertEqual([c["contract"] for c in self.packet(rows)["candidates"]], ["INTC3", "INTC2", "INTC1"])

    def test_model_cannot_add_contract_or_modify_numbers_or_hash(self):
        packet = self.packet()
        self.assertEqual(validate_advice_result(self.result(packet), packet)["outcome"], "compare")
        for field, value in (("candidate_id", "C999"), ("required_cash", "0")):
            result = self.result(packet)
            result["comparisons"][0][field] = value
            with self.assertRaises(ValueError):
                validate_advice_result(result, packet)
        result = self.result(packet)
        result["input_hash"] = "other"
        with self.assertRaises(ValueError):
            validate_advice_result(result, packet)

    def test_empty_allowlist_requires_no_trade_and_limits_response(self):
        packet = self.packet([])
        result = self.result(packet)
        with self.assertRaises(ValueError):
            validate_advice_result(result, packet)
        result.update(outcome="no_trade", comparisons=[])
        validate_advice_result(result, packet)
        result["summary"] = "x" * 1501
        with self.assertRaises(ValueError):
            validate_advice_result(result, packet)


class AdvicePageTests(TestCase):
    metadata = fixtures.WheelAnalysisServiceTests.metadata

    def setUp(self):
        self.family = Family.objects.create(name="Advice family")
        self.user = get_user_model().objects.create_user(username="advice-member")
        member = FamilyMember.objects.create(family=self.family, user=self.user, display_name="Member")
        bank = BankAccount.objects.create(family=self.family, member=member, account_name="盈透证券", supports_investment=True)
        self.account = InvestmentAccount.objects.create(bank_account=bank)
        stock = Security.objects.create(symbol="INTC", name="Intel", market="US", currency="USD", asset_type=Security.TYPE_STOCK)
        WheelPolicy.objects.create(family=self.family, account=self.account, underlying=stock)
        WheelBrokerAccountSnapshot.objects.create(family=self.family, account=self.account,
            source_kind="manual_file", source_reference="fixture", currency="USD", settled_cash=20000,
            unsettled_cash=0, reserved_cash=0, nav=20000, margin_loan_balance=0, uses_margin=False,
            positions_summary={}, open_obligations={}, source_as_of=timezone.now(), data_status="complete")
        probe = fixtures.WheelAnalysisServiceTests.probe_result(self)
        probe["symbol"] = "US.INTC"
        probe["representative_contracts"][0]["code"] = "US.INTC_TEST_P"
        probe["representative_contracts"][0]["strike_price"] = "30"
        self.decision = persist_probe_symbol(family=self.family, account=self.account, symbol_result=probe)
        self.url = reverse("option_wheel:advice_preview", args=[self.decision.pk])

    def test_readonly_page_and_explicit_unconnected_state(self):
        self.client.force_login(self.user)
        counts = (WheelDecision.objects.count(), WheelBrokerAccountSnapshot.objects.count())
        response = self.client.get(self.url)
        self.assertContains(response, "AI 尚未配置就绪")
        self.assertContains(response, "尚未发送")
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(counts, (WheelDecision.objects.count(), WheelBrokerAccountSnapshot.objects.count()))
        self.assertEqual(self.client.post(self.url).status_code, 405)

    def test_family_isolation_and_login(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)
        other = get_user_model().objects.create_user(username="other-advice")
        FamilyMember.objects.create(family=Family.objects.create(name="Other"), user=other, display_name="Other")
        self.client.force_login(other)
        self.assertEqual(self.client.get(self.url).status_code, 404)


class AdviceJobTests(AdvicePageTests):
    def setUp(self):
        super().setUp()
        from ai_analysis.models import AiProvider
        self.user.is_superuser = True
        self.user.save(update_fields=["is_superuser"])
        self.env = patch.dict("os.environ", {"WHEEL_TEST_API_KEY": "test-only-not-a-real-key"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.provider = AiProvider.objects.create(name="Test DeepSeek", provider_type="openai_compatible",
            model_name="deepseek-v4-flash", base_url="https://api.deepseek.com", extra_data={
                "api_key_env_var": "WHEEL_TEST_API_KEY", "intelligence_max_input_characters": 20000,
                "intelligence_max_output_tokens": 1800, "intelligence_input_usd_per_million": "0.14",
                "intelligence_output_usd_per_million": "0.28", "intelligence_max_estimated_usd": "0.01"})
        self.client.force_login(self.user)
        self.generate_url = reverse("option_wheel:advice_generate", args=[self.decision.pk])

    def submit(self):
        token = self.client.get(self.url).context["consent_token"]
        with patch("option_wheel.advice_jobs.launch_advice"), self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(self.generate_url, {"confirm_public_ai": "yes", "consent_token": token})
        from ai_analysis.models import AiAnalysisRequest
        self.assertEqual(response.status_code, 303)
        return AiAnalysisRequest.objects.filter(module="option_wheel").latest("pk")

    def reply(self, request):
        packet = request.sanitized_input
        return {"result": {"schema": SCHEMA, "input_hash": packet["input_hash"], "outcome": "no_trade",
            "summary": "本份证据暂不操作。", "comparisons": [], "limitations": ["新闻未提供。"]},
            "usage": {"prompt_tokens": 1000, "completion_tokens": 100}}

    def test_readonly_page_and_explicit_unconnected_state(self):
        # Override inherited no-provider expectation; GET only previews authorization.
        from ai_analysis.models import AiAnalysisRequest
        self.assertContains(self.client.get(self.url), "生成 DeepSeek 解释")
        self.assertFalse(AiAnalysisRequest.objects.filter(module="option_wheel").exists())

    def test_consent_admin_and_nonce_deduplication(self):
        from ai_analysis.models import AiAnalysisRequest
        self.assertEqual(self.client.post(self.generate_url).status_code, 400)
        payload = {"confirm_public_ai": "yes", "consent_token": self.client.get(self.url).context["consent_token"]}
        with patch("option_wheel.advice_jobs.launch_advice") as launch, self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.client.post(self.generate_url, payload).status_code, 303)
            self.assertEqual(self.client.post(self.generate_url, payload).status_code, 303)
        self.assertEqual(AiAnalysisRequest.objects.filter(module="option_wheel").count(), 1)
        launch.assert_called_once()
        self.user.is_superuser = False
        self.user.save(update_fields=["is_superuser"])
        self.assertEqual(self.client.post(self.generate_url, payload).status_code, 403)

    def test_save_result_cost_and_reuse_without_second_api_call(self):
        from option_wheel.advice_jobs import run_advice
        request = self.submit()
        with patch("option_wheel.advice_jobs.subprocess.run", return_value=Obj(returncode=0, stdout=json.dumps(self.reply(request)))) as run:
            run_advice(request.pk)
            run_advice(request.pk)
        run.assert_called_once()
        request.refresh_from_db()
        self.assertEqual(request.status, "success")
        self.assertEqual(request.result.tokens_used, 1100)
        self.assertEqual(request.result.cost_estimate, Decimal("0.000168"))
        self.assertEqual(self.submit().pk, request.pk)
        self.assertContains(self.client.get(self.url), "本份证据下暂不操作")

    def test_provider_change_blocks_before_external_call(self):
        from option_wheel.advice_jobs import run_advice
        request = self.submit()
        self.provider.is_active = False
        self.provider.save(update_fields=["is_active"])
        with patch("option_wheel.advice_jobs.subprocess.run") as run:
            run_advice(request.pk)
        run.assert_not_called()
        request.refresh_from_db()
        self.assertEqual(request.status, "failed")

    def test_estimated_cost_limit_and_active_request_guard(self):
        from option_wheel.advice_jobs import check_request_cost, provider_configuration, AdviceError
        _, config = provider_configuration(self.provider)
        with self.assertRaises(AdviceError):
            check_request_cost({"input": "x" * 20001}, config)
        config["max_cost"] = "0.000001"
        with self.assertRaises(AdviceError):
            check_request_cost({"input": "small"}, config)
        request = self.submit()
        token = self.client.get(self.url).context["consent_token"]
        response = self.client.post(self.generate_url, {"confirm_public_ai": "yes", "consent_token": token})
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "已有 AI 分析正在运行", status_code=400)

    def test_status_request_id_cannot_read_other_decision(self):
        request = self.submit()
        self.assertEqual(self.client.get(self.url + '?request=999999').status_code, 404)
        self.assertEqual(self.client.get(self.url + '?request=bad').status_code, 400)

    def test_timeout_get_never_mutates_or_retries(self):
        from ai_analysis.models import AiAnalysisRequest
        request = self.submit()
        AiAnalysisRequest.objects.filter(pk=request.pk).update(created_at=timezone.now()-timedelta(minutes=3))
        with patch("option_wheel.advice_jobs.launch_advice") as launch:
            response = self.client.get(self.url, HTTP_ACCEPT="application/json")
        launch.assert_not_called()
        self.assertEqual(response.json()["status"], "interrupted")
        request.refresh_from_db()
        self.assertEqual(request.status, "pending")

    def test_bad_result_and_permission_revocation_not_saved(self):
        from option_wheel.advice_jobs import run_advice
        from ai_analysis.models import AiAnalysisResult
        request = self.submit()
        bad = self.reply(request)
        bad["result"]["input_hash"] = "wrong"
        with patch("option_wheel.advice_jobs.subprocess.run", return_value=Obj(returncode=0, stdout=json.dumps(bad))):
            run_advice(request.pk)
        self.assertFalse(AiAnalysisResult.objects.filter(request=request).exists())
        request = self.submit()
        def revoke(*args, **kwargs):
            self.user.is_active = False
            self.user.save(update_fields=["is_active"])
            return Obj(returncode=0, stdout=json.dumps(self.reply(request)))
        with patch("option_wheel.advice_jobs.subprocess.run", side_effect=revoke):
            run_advice(request.pk)
        self.assertFalse(AiAnalysisResult.objects.filter(request=request).exists())

    def test_transport_only_sends_public_packet_and_rejects_redirect(self):
        from option_wheel.advice_transport import call_deepseek, NoRedirect
        request = self.submit()
        http_response = {"choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps(self.reply(request)["result"])}}], "usage": {"prompt_tokens": 12, "completion_tokens": 8}}
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(http_response).encode()
        with patch("option_wheel.advice_transport.build_opener") as opener:
            opener.return_value.open.return_value = response
            call_deepseek(request)
        http = opener.return_value.open.call_args.args[0]
        self.assertEqual(http.full_url, "https://api.deepseek.com/chat/completions")
        body = json.loads(http.data)
        self.assertNotIn("test-only-not-a-real-key", str(body))
        self.assertNotIn("盈透证券", str(body))
        self.assertNotIn("config_hash", str(body))
        self.assertEqual(body["thinking"], {"type": "disabled"})
        with self.assertRaises(ValueError):
            NoRedirect().redirect_request(None, None, None, None, None, "https://other.example")

    def test_ai_index_does_not_expose_other_household(self):
        from ai_analysis.models import AiAnalysisRequest
        other_member = FamilyMember.objects.create(family=Family.objects.create(name="Secret family"), display_name="Secret member")
        AiAnalysisRequest.objects.create(family=other_member.family, member=other_member, module="PRIVATE_OTHER_HOUSEHOLD", prompt="private")
        self.assertNotContains(self.client.get(reverse("ai_analysis:index")), "PRIVATE_OTHER_HOUSEHOLD")
