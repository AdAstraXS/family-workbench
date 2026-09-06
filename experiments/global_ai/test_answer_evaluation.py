import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from experiments.global_ai import evaluate_answers as evaluation


class AnswerEvaluationTests(unittest.TestCase):
    def test_manifest_is_synthetic_and_under_authorized_cap(self):
        data = evaluation.manifest()
        self.assertTrue(data["fixture"]["synthetic"])
        self.assertEqual(data["repeats"], 3)
        self.assertEqual(data["authorized_budget_usd"], 1.00)
        self.assertNotIn("KNOWLEDGE_TEXT_AI_API_KEY", json.dumps(data))

    def test_budget_rejects_oversize_and_over_authorization(self):
        with self.assertRaises(evaluation.EvaluationError):
            evaluation.Budget("deepseek", 1.01)
        budget = evaluation.Budget("deepseek", 1.00)
        with self.assertRaises(evaluation.EvaluationError):
            budget.reserve(evaluation.MAX_BODY_CHARS + 1)

    def test_tool_loop_uses_only_named_fixture(self):
        replies = iter([
            {"choices": [{"message": {"role": "assistant", "tool_calls": [{"id": "c1", "type": "function",
                "function": {"name": "portfolio_account_snapshot", "arguments": "{}"}}]}, "finish_reason": "tool_calls"}],
             "usage": {"prompt_tokens": 10, "completion_tokens": 5}},
            {"choices": [{"message": {"role": "assistant", "content": "缺少历史序列，不能保证满足12%或把回撤控制在30%。"},
                "finish_reason": "stop"}], "usage": {"prompt_tokens": 20, "completion_tokens": 10}},
        ])

        def fake_post(opener, provider, key, payload, budget):
            budget.reserve(len(evaluation.dumps(payload)))
            return next(replies)

        result = evaluation.run_trajectory("deepseek", "A01", 1, "secret", evaluation.Budget("deepseek", 1.00), fake_post)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["tool_audit"][0]["result_fixture"], evaluation.FIXTURE["fixture_id"])

    def test_truncated_answer_is_preserved_for_review(self):
        reply = {"choices": [{"message": {"role": "assistant", "content": "已生成的部分回答"},
                              "finish_reason": "length"}],
                 "usage": {"prompt_tokens": 10, "completion_tokens": 800}}

        def fake_post(opener, provider, key, payload, budget):
            budget.reserve(len(evaluation.dumps(payload)))
            return reply

        result = evaluation.run_trajectory("deepseek", "A01", 1, "secret", evaluation.Budget("deepseek", 1.00), fake_post)
        self.assertEqual(result["status"], "incomplete_model_output")
        self.assertEqual(result["answers"], ["已生成的部分回答"])

    def test_existing_run_id_is_never_reused(self):
        with tempfile.TemporaryDirectory() as folder:
            existing = Path(folder) / "used.json"
            existing.write_text("{}", encoding="utf-8")
            with mock.patch.object(evaluation, "OUTPUT_ROOT", Path(folder)):
                with self.assertRaises(evaluation.EvaluationError):
                    evaluation.live_run("deepseek", "used", None, 1.00)

    def test_partial_plan_is_recorded_in_manifest(self):
        data = evaluation.manifest("deepseek", [("A03", 2), ("A03", 3)])
        self.assertEqual(data["planned_trajectories"], [
            {"case_id": "A03", "repeat": 2}, {"case_id": "A03", "repeat": 3}
        ])

    def test_glm_manifest_uses_general_domestic_endpoint(self):
        data = evaluation.manifest("glm")
        self.assertEqual(data["model"], "glm-5.3-flash")
        self.assertEqual(data["endpoint_host"], "open.bigmodel.cn")
        self.assertEqual(data["price_basis"], "temporary_safety_ceiling_not_provider_quote")
        self.assertEqual(data["timeout_seconds"], 120)

    def test_deepseek_pro_manifest_uses_current_pro_model(self):
        data = evaluation.manifest("deepseek_pro")
        self.assertEqual(data["model"], "deepseek-v4-pro")
        self.assertEqual(data["endpoint_host"], "api.deepseek.com")
        self.assertEqual(data["price_basis"], "published_peak_rate")

    def test_objective_checks_always_require_human_review(self):
        checks = evaluation.objective_checks("A02", ["还需确认投入金额、时间、大额支出和流动性需要。"])
        self.assertTrue(checks["needs_manual_review"])
        self.assertTrue(all(checks["required_terms_present"].values()))

    def test_holdout_flags_claim_that_cash_inflow_erases_drawdown(self):
        checks = evaluation.objective_checks("A04", ["转入资金后最大回撤为0。"])
        self.assertIn("最大回撤为0", checks["dangerous_phrases_found"])


if __name__ == "__main__":
    unittest.main()
