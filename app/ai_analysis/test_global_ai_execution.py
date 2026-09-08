import os
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from family_core.models import Family, FamilyMember

from .global_ai_jobs import execute_model_loop, provider_configuration
from .global_ai_services import claim_global_ai_request, complete_global_ai_request
from .models import (
    AiAnalysisRequest,
    AiConversation,
    AiConversationMessage,
    AiOutboundAuthorization,
    AiProvider,
)


def configured_provider():
    provider = AiProvider.objects.filter(name="DeepSeek 全局 AI").first()
    if provider is None:
        provider = AiProvider.objects.create(name="DeepSeek 全局 AI")
    provider.provider_type = "openai_compatible"
    provider.base_url = "https://api.deepseek.com"
    provider.model_name = "deepseek-v4-pro"
    provider.execution_location = AiProvider.LOCATION_CLOUD
    provider.is_active = True
    provider.extra_data = {
        "global_ai_enabled": True,
        "api_key_env_var": "TEST_DEEPSEEK_KEY",
        "global_ai_input_usd_per_million": "1.32",
        "global_ai_output_usd_per_million": "3.96",
        "global_ai_max_estimated_usd": "0.10",
        "global_ai_max_input_characters": 20000,
        "global_ai_max_output_tokens": 1000,
        "global_ai_max_http_requests": 4,
        "global_ai_timeout_seconds": 45,
        "global_ai_daily_request_limit": 20,
    }
    provider.save()
    return provider


class GlobalAiExecutionTests(TestCase):
    def setUp(self):
        self.family = Family.objects.create(name="执行测试家庭", base_currency="CNY")
        self.user = get_user_model().objects.create_user(username="global-executor")
        self.member = FamilyMember.objects.create(
            family=self.family, user=self.user, display_name="成员"
        )
        self.conversation = AiConversation.objects.create(
            family=self.family, member=self.member, title="测试对话"
        )
        self.provider = configured_provider()
        for data_type in ("conversation", "financial"):
            AiOutboundAuthorization.objects.create(
                family=self.family,
                member=self.member,
                provider=self.provider,
                data_type=data_type,
                is_allowed=True,
            )

    def test_provider_configuration_uses_environment_secret(self):
        with patch.dict(os.environ, {"TEST_DEEPSEEK_KEY": "secret-for-test"}):
            provider, config = provider_configuration(self.provider)
        self.assertEqual(provider, self.provider)
        self.assertEqual(config["model"], "deepseek-v4-pro")
        self.assertNotIn("secret-for-test", str(config))

    def test_tool_loop_uses_host_dispatch_and_returns_auditable_evidence(self):
        AiConversationMessage.objects.create(
            conversation=self.conversation, sequence=1, role="user", content="查看整体资产"
        )
        request = AiAnalysisRequest.objects.create(
            family=self.family,
            member=self.member,
            conversation=self.conversation,
            provider=self.provider,
            module="global_ai",
            analysis_type="chat_v1",
            prompt="查看整体资产",
            status=AiAnalysisRequest.STATUS_RUNNING,
            execution_token="token",
        )
        _provider, config = None, {
            "model": "deepseek-v4-pro",
            "max_output_tokens": 1000,
            "max_http_requests": 4,
            "max_input_characters": 20000,
            "input_price": "1.32",
            "output_price": "3.96",
            "max_cost": "0.10",
        }
        replies = iter([
            {
                "choices": [{"finish_reason": "tool_calls", "message": {
                    "role": "assistant", "content": "", "tool_calls": [{
                        "id": "call-1", "type": "function", "function": {
                            "name": "ledger_asset_snapshot", "arguments": "{}"
                        }
                    }]
                }}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 8},
            },
            {
                "choices": [{"finish_reason": "stop", "message": {
                    "role": "assistant", "content": "账本正式快照显示整体资产为 100 元。"
                }}],
                "usage": {"prompt_tokens": 30, "completion_tokens": 12},
            },
        ])
        tool_result = {
            "tool_name": "ledger_asset_snapshot",
            "data_type": "financial",
            "result": {"module": "ledger", "total_base_amount": "100.0000"},
            "evidence_refs": [{"kind": "ledger_snapshot", "snapshot_id": 7}],
        }
        with patch("ai_analysis.global_ai_jobs.dispatch_read_tool", return_value=tool_result) as dispatch:
            result = execute_model_loop(request, config, post_json=lambda payload, config: next(replies))
        dispatch.assert_called_once()
        self.assertEqual(result["data_types"], ["financial"])
        self.assertEqual(result["evidence_refs"], tool_result["evidence_refs"])
        self.assertEqual(result["tokens_used"], 70)

    def test_completion_adds_one_assistant_message_and_rejects_late_cancelled_result(self):
        request = AiAnalysisRequest.objects.create(
            family=self.family,
            member=self.member,
            conversation=self.conversation,
            provider=self.provider,
            module="global_ai",
            analysis_type="chat_v1",
            prompt="你好",
        )
        token = claim_global_ai_request(request_id=request.pk)
        complete_global_ai_request(
            request_id=request.pk,
            execution_token=token,
            result_text="你好。",
            tokens_used=2,
            cost_estimate="0.000001",
        )
        answer = self.conversation.messages.get(role="assistant")
        self.assertEqual(answer.content, "你好。")
        self.assertEqual(request.result.result_json["message_id"], answer.pk)


class GlobalAiAskViewTests(TestCase):
    def setUp(self):
        self.family = Family.objects.create(name="网页测试家庭", base_currency="CNY")
        self.user = get_user_model().objects.create_user(username="global-web-user")
        self.member = FamilyMember.objects.create(
            family=self.family, user=self.user, display_name="网页成员"
        )
        self.conversation = AiConversation.objects.create(
            family=self.family, member=self.member, title="网页对话"
        )
        self.provider = configured_provider()
        AiOutboundAuthorization.objects.create(
            family=self.family,
            member=self.member,
            provider=self.provider,
            data_type="conversation",
            is_allowed=True,
        )
        self.client.force_login(self.user)

    @patch("ai_analysis.views.launch_global_ai_request")
    @patch("ai_analysis.views.provider_configuration")
    def test_ask_is_idempotent_and_launches_only_once(self, configuration, launch):
        configuration.return_value = (self.provider, {"fingerprint": "config-1"})
        url = reverse("ai_analysis:conversation_ask", args=[self.conversation.pk])
        payload = {"content": "请总结资产", "idempotency_key": "same-key"}
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(url, payload)
        self.assertRedirects(response, reverse("ai_analysis:conversation", args=[self.conversation.pk]))
        self.client.post(url, payload)
        self.client.post(url, {"content": "并发的另一问", "idempotency_key": "new-key"})
        self.assertEqual(AiAnalysisRequest.objects.filter(module="global_ai").count(), 1)
        self.assertEqual(self.conversation.messages.filter(role="user").count(), 1)
        launch.assert_called_once()

    def test_member_can_change_only_own_outbound_grants(self):
        other_user = get_user_model().objects.create_user(username="global-other")
        other = FamilyMember.objects.create(family=self.family, user=other_user, display_name="其他成员")
        AiOutboundAuthorization.objects.create(
            family=self.family, member=other, provider=self.provider,
            data_type="financial", is_allowed=True,
        )
        response = self.client.post(
            reverse("ai_analysis:outbound_authorization_update"),
            {"allowed_data_types": ["conversation", "knowledge"]},
        )
        self.assertRedirects(response, reverse("ai_analysis:index"))
        own = set(AiOutboundAuthorization.objects.filter(member=self.member, is_allowed=True).values_list("data_type", flat=True))
        self.assertEqual(own, {"conversation", "knowledge"})
        self.assertTrue(AiOutboundAuthorization.objects.get(member=other, data_type="financial").is_allowed)
