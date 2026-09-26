import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch
from uuid import uuid4
from zoneinfo import ZoneInfo

from django.core import signing

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from ai_analysis.models import AiAnalysisRequest, AiAnalysisResult, AiProvider
from family_core.models import Family, FamilyMember
from option_wheel.models import WheelAnalysisJob, WheelWatchItem
from option_wheel.jobs import run_job
from option_wheel.screen_advice import SCHEMA, build_packets, context_for_job, validate_result
from option_wheel.screen_advice_jobs import enqueue_for_screening, run_screen_advice


class ScreenAdviceTests(TestCase):
    def setUp(self):
        self.family = Family.objects.create(name="AI screening family")
        self.user = get_user_model().objects.create_user(
            username="screen-ai-owner", is_superuser=True, is_staff=True)
        FamilyMember.objects.create(family=self.family, user=self.user, display_name="Owner")
        WheelWatchItem.objects.create(family=self.family, symbol="INTC")
        self.provider = AiProvider.objects.create(
            name="Test DeepSeek", provider_type="openai_compatible",
            model_name="deepseek-v4-flash", base_url="https://api.deepseek.com",
            extra_data={
                "api_key_env_var": "WHEEL_SCREEN_TEST_KEY", "intelligence_max_input_characters": 20000,
                "intelligence_max_output_tokens": 1800, "intelligence_input_usd_per_million": "0.14",
                "intelligence_output_usd_per_million": "0.28", "intelligence_max_estimated_usd": "0.01",
            },
        )
        self.env = patch.dict("os.environ", {"WHEEL_SCREEN_TEST_KEY": "test-key-only"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.job = WheelAnalysisJob.objects.create(
            family=self.family, requested_by=self.user, status="saved",
            selection={"mode": "screening_v2", "symbols": ["INTC"],
                       "analysis_date": "2026-09-24", "target_expiration": "2026-10-02",
                       "premium_min": "100", "premium_max": "500", "ai_enabled": True},
            screening_results=[{
                "symbol": "INTC", "code": "US.INTC-P1", "strategy": "PUT", "premium": "150.00",
                "strike": "30", "break_even": "28.50", "iv": "42", "delta": "-0.18",
                "underlying_iv_percentile": "65", "probability": "18", "annualized_premium_rate": "12",
                "risks": ["到期前跨财报"], "account": "PRIVATE ACCOUNT", "cost": "123456",
            }],
            expires_at=timezone.now() + timedelta(minutes=12), finished_at=timezone.now(),
        )
        self.client.force_login(self.user)

    def _enqueue(self):
        with patch("option_wheel.screen_advice_jobs._launch") as launch, self.captureOnCommitCallbacks(execute=True):
            enqueue_for_screening(self.job.pk)
        return launch

    def test_packet_is_public_and_output_must_cover_exact_candidates(self):
        packet = build_packets(self.job)[0]
        serialized = json.dumps(packet)
        self.assertNotIn("PRIVATE ACCOUNT", serialized)
        self.assertNotIn("123456", serialized)
        self.assertNotIn("cash", serialized)
        self.assertNotIn("nav", serialized)
        valid = {"schema": SCHEMA, "input_hash": packet["input_hash"],
                 "advice": [{"candidate_id": "C1", "text": "权利金与价内风险并看；财报前可暂不操作。"}]}
        self.assertEqual(validate_result(valid, packet), valid)
        with self.assertRaises(ValueError):
            validate_result({**valid, "advice": []}, packet)
        with self.assertRaises(ValueError):
            validate_result({**valid, "advice": [{"candidate_id": "C2", "text": "错"}]}, packet)

    def test_all_visible_rows_are_partitioned_without_omission(self):
        sample = self.job.screening_results[0]
        self.job.screening_results = [{**sample, "code": f"US.INTC-P{i}"} for i in range(33)]
        self.job.save(update_fields=["screening_results"])
        packets = build_packets(self.job)
        self.assertEqual([len(packet["candidates"]) for packet in packets], [32, 1])
        self.assertEqual(packets[1]["candidates"][0]["candidate_id"], "C33")

    def test_covered_call_input_does_not_reveal_cost_indirectly(self):
        self.job.screening_results = [{
            "symbol": "INTC", "code": "US.INTC-C1", "strategy": "CALL",
            "premium": "120.00", "strike": "30", "break_even": "432.10",
            "annualized_premium_rate": "7.65", "probability": "25",
            "account": "PRIVATE ACCOUNT", "cost": "433.30",
            "risks": ["Call 行权价低于该账户持股成本", "到期前跨财报"],
        }]
        self.job.save(update_fields=["screening_results"])
        packet = build_packets(self.job)[0]
        serialized = json.dumps(packet, ensure_ascii=False)
        self.assertNotIn("432.10", serialized)
        self.assertNotIn("7.65", serialized)
        self.assertNotIn("433.30", serialized)
        self.assertNotIn("PRIVATE ACCOUNT", serialized)
        self.assertNotIn("持股成本", serialized)
        self.assertIn("到期前跨财报", serialized)

    def test_opt_in_enqueue_deduplicates_and_keeps_rule_only_when_disabled(self):
        launch = self._enqueue()
        self.assertEqual(launch.call_count, 1)
        self._enqueue()
        self.assertEqual(AiAnalysisRequest.objects.filter(analysis_type=SCHEMA).count(), 1)
        request = AiAnalysisRequest.objects.get(analysis_type=SCHEMA)
        self.assertEqual(request.scope["job_id"], str(self.job.pk))
        self.assertEqual(request.scope["consent"], "screen_checkbox_public_market_total_usd_0.02_v1")
        self.job.selection["ai_enabled"] = False
        self.job.save(update_fields=["selection"])
        self.assertEqual(context_for_job(self.job)["status"], "disabled")

    def test_cost_limit_fails_closed_without_losing_rule_results(self):
        with patch("option_wheel.screen_advice_jobs.MAX_TOTAL_ESTIMATED_USD", Decimal("0.000001")):
            enqueue_for_screening(self.job.pk)
        self.assertFalse(AiAnalysisRequest.objects.filter(analysis_type=SCHEMA).exists())
        self.job.refresh_from_db()
        self.assertIn("AI 建议未生成", self.job.message)
        self.assertEqual(context_for_job(self.job)["status"], "failed")
        self.assertEqual(len(context_for_job(self.job)["rows"]), 1)

    def test_saved_ai_is_shown_separately_on_home_and_history(self):
        self._enqueue()
        request = AiAnalysisRequest.objects.get(analysis_type=SCHEMA)
        packet = request.sanitized_input
        reply = {"result": {"schema": SCHEMA, "input_hash": packet["input_hash"],
                            "advice": [{"candidate_id": "C1", "text": "AI 比较：财报前谨慎。"}]},
                 "usage": {"prompt_tokens": 1000, "completion_tokens": 100}}
        completed = type("Completed", (), {"returncode": 0, "stdout": json.dumps(reply)})()
        with patch("option_wheel.screen_advice_jobs.subprocess.run", return_value=completed) as transport:
            run_screen_advice(request.pk)
            run_screen_advice(request.pk)
        transport.assert_called_once()
        request.refresh_from_db()
        self.assertEqual(request.status, "success")
        self.assertEqual(AiAnalysisResult.objects.filter(request=request).count(), 1)
        for url in (reverse("option_wheel:index"), reverse("option_wheel:job_detail", args=[self.job.pk])):
            page = self.client.get(url)
            self.assertContains(page, "规则建议")
            self.assertContains(page, "DeepSeek AI 建议")
            self.assertContains(page, "AI 比较：财报前谨慎。")

    def test_transport_uses_screen_schema_and_public_packet(self):
        from option_wheel.advice_transport import call_deepseek

        self._enqueue()
        request = AiAnalysisRequest.objects.get(analysis_type=SCHEMA)
        packet = request.sanitized_input
        reply = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({
            "schema": SCHEMA, "input_hash": packet["input_hash"],
            "advice": [{"candidate_id": "C1", "text": "比较风险与权利金。"}],
        })}}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(reply).encode()
        with patch("option_wheel.advice_transport.build_opener") as opener:
            opener.return_value.open.return_value = response
            result = call_deepseek(request)
        payload = json.loads(opener.return_value.open.call_args.args[0].data)
        self.assertEqual(result["result"]["advice"][0]["candidate_id"], "C1")
        self.assertEqual(json.loads(payload["messages"][1]["content"]), packet)
        self.assertNotIn("PRIVATE ACCOUNT", payload["messages"][1]["content"])

    def test_default_analysis_page_does_not_start_ai(self):
        page = self.client.get(reverse("option_wheel:index"))
        self.assertContains(page, "开启本次 AI 建议")
        self.assertFalse(AiAnalysisRequest.objects.exists())

    def test_analysis_checkbox_is_saved_only_when_selected(self):
        today = timezone.now().astimezone(ZoneInfo("America/New_York")).date()
        friday = today + timedelta(days=(4 - today.weekday()) % 7 or 7)
        token = signing.dumps({"family": self.family.pk, "key": str(uuid4())}, salt="wheel-live-job-v1")
        with patch("option_wheel.jobs.launch_job"), self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse("option_wheel:analyze"), {
                "symbols": ["INTC"], "request_token": token, "expiry_choice": "custom",
                "custom_expiry": friday.isoformat(), "premium_min": "100", "premium_max": "500",
                "ai_enabled": "on",
            }, HTTP_ACCEPT="application/json")
        self.assertEqual(response.status_code, 202)
        submitted = WheelAnalysisJob.objects.get(pk=response.json()["id"])
        self.assertTrue(submitted.selection["ai_enabled"])
        self.assertFalse(AiAnalysisRequest.objects.exists())

    def test_saved_futu_analysis_enqueues_ai_after_commit(self):
        sample = self.job.screening_results[0]
        self.job.status = "queued"
        self.job.screening_results = []
        self.job.save(update_fields=["status", "screening_results"])
        vendor = [{"symbol": "US.INTC", "representative_contracts": [{"code": "US.INTC-P1"}]}]
        with (patch("option_wheel.jobs.fetch_probe", return_value=vendor),
              patch("option_wheel.screening.compare_probe_rows", return_value=[sample]),
              patch("option_wheel.screen_advice_jobs._launch"),
              self.captureOnCommitCallbacks(execute=True)):
            run_job(self.job.pk)
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, "saved")
        self.assertEqual(AiAnalysisRequest.objects.filter(
            analysis_type=SCHEMA, scope__job_id=str(self.job.pk)).count(), 1)
