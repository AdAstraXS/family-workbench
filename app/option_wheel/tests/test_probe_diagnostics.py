from django.test import SimpleTestCase

from option_wheel.probe_diagnostics import probe_failure_summary


class ProbeDiagnosticTests(SimpleTestCase):
    def test_classifies_provider_hint_without_echoing_secrets(self):
        result = {
            "errors": [{"source": "subscription_before", "category": "provider_error",
                        "error": "not logged in account 123456 password=sensitive-secret"}],
            "market_state": {"market_us": "MORNING"},
            "subscription": {"cleanup_status": "restored"},
        }
        text = probe_failure_summary(result, {"TSLA"})
        self.assertIn("订阅额度预检：行情服务拒绝请求（服务提示未登录）", text)
        self.assertIn("市场状态：正常交易时段；订阅清理：已恢复", text)
        self.assertNotIn("123456", text)
        self.assertNotIn("sensitive-secret", text)

    def test_untrusted_values_are_never_echoed(self):
        secret = "SECRET<script>alert(1)</script>"
        result = {
            "errors": [secret, {"source": secret, "category": secret, "error": secret},
                       "optional_method_unsupported:" + secret],
            "market_state": {"market_us": secret},
            "subscription": {"cleanup_status": secret},
            "symbols": [{"symbol": secret, "status": "failed", "errors": [secret]}],
        }
        text = probe_failure_summary(result, {"TSLA"})
        self.assertNotIn(secret, text)
        self.assertNotIn("<script>", text)
        self.assertIn("原始内容已隐藏", text)

    def test_nested_errors_and_missing_methods(self):
        result = {
            "errors": ["optional_method_unsupported:get_earnings_calendar"],
            "symbols": [{"symbol": "US.TSLA", "status": "partial", "errors": [],
                         "history": {"status": "error", "issue": {
                             "source": "request_history_kline", "category": "signature_mismatch"}},
                         "representative_contracts": [{"analytics": {"probability": {
                             "status": "unsupported", "issue": {
                                 "source": "get_option_exercise_probability", "category": "method_unsupported"}}}}]}],
        }
        text = probe_failure_summary(result, {"TSLA"})
        self.assertIn("SDK 不支持：财报日历", text)
        self.assertIn("TSLA：历史价格：SDK 参数不兼容", text)
        self.assertIn("TSLA：概率数据：SDK 不支持接口", text)

    def test_dynamic_candidate_limit_has_safe_diagnostic(self):
        result = {
            "errors": ["dynamic candidate limit exceeded: 30>27"],
            "market_state": {},
            "subscription": {},
        }
        text = probe_failure_summary(result, {"TSLA"})
        self.assertIn("本批候选数量超过系统上限", text)
        self.assertNotIn("30>27", text)

    def test_fallback_and_bounded_output(self):
        self.assertIn("未返回可分类的失败项", probe_failure_summary({}, {"TSLA"}))
        result = {"errors": ["unknown"] * 100, "symbols": []}
        text = probe_failure_summary(result, {"TSLA"})
        self.assertEqual(text.count("原始内容已隐藏"), 1)
        self.assertLess(len(text), 1500)
