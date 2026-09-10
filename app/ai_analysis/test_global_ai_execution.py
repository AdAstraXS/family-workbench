import os
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from family_core.models import Family, FamilyMember

from .global_ai_jobs import execute_model_loop, provider_configuration
from .global_ai_services import claim_global_ai_request, complete_global_ai_request
from .forms import GlobalAiPromptForm
from .models import (
    AiAnalysisRequest,
    AiConversation,
    AiConversationMessage,
    AiFamilyOutboundAuthorization,
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
        "global_ai_timeout_seconds": 45,
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
        self.assertNotIn("max_http_requests", config)
        self.assertNotIn("max_output_tokens", config)
        self.assertNotIn("max_cost", config)
        self.assertNotIn("max_input_characters", config)
        self.assertEqual(config["loop_timeout_seconds"], 180)
        self.assertNotIn("secret-for-test", str(config))

    def test_prompt_form_has_no_product_length_cap(self):
        form = GlobalAiPromptForm(data={"content": "问题" * 3000, "idempotency_key": "k"})
        self.assertTrue(form.is_valid(), form.errors)

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
            "input_price": "1.32",
            "output_price": "3.96",
            "loop_timeout_seconds": 180,
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
                "usage": {"prompt_tokens": 100000, "completion_tokens": 80000},
            },
            {
                "choices": [{"finish_reason": "stop", "message": {
                    "role": "assistant", "content": "账本正式快照显示整体资产为 100 元。"
                }}],
                "usage": {"prompt_tokens": 100000, "completion_tokens": 120000},
            },
        ])
        tool_result = {
            "tool_name": "ledger_asset_snapshot",
            "data_type": "financial",
            "result": {"module": "ledger", "total_base_amount": "100.0000"},
            "evidence_refs": [{"kind": "ledger_snapshot", "snapshot_id": 7}],
        }
        payloads = []

        def post_json(payload, _config):
            payloads.append(payload)
            return next(replies)

        with patch("ai_analysis.global_ai_jobs.dispatch_read_tool", return_value=tool_result) as dispatch:
            result = execute_model_loop(request, config, post_json=post_json)
        dispatch.assert_called_once()
        self.assertNotIn("max_tokens", payloads[0])
        self.assertEqual(result["data_types"], ["financial"])
        self.assertEqual(result["evidence_refs"], tool_result["evidence_refs"])
        self.assertEqual(result["tokens_used"], 400000)
        self.assertGreater(result["cost"], Decimal("0.10"))

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
            message_data_types=["financial"],
            evidence_refs=[
                {"kind": "ledger_snapshot", "snapshot_id": 7},
                {
                    "kind": "ledger_cashflow_budget",
                    "year": 2026,
                    "as_of_date": "2026-09-10",
                    "fingerprint": "0" * 64,
                },
            ],
        )
        answer = self.conversation.messages.get(role="assistant")
        self.assertEqual(answer.content, "你好。")
        self.assertEqual(request.result.result_json["message_id"], answer.pk)
        self.client.force_login(self.user)
        response = self.client.get(
            reverse("ai_analysis:conversation", args=[self.conversation.pk])
        )
        self.assertContains(response, "查看回答依据（2）")
        self.assertContains(response, "账本正式资产快照 #7")
        self.assertContains(response, "账本 2026 年收支与预算（截至 2026-09-10）")


class GlobalAiAskViewTests(TestCase):
    def setUp(self):
        self.family = Family.objects.create(name="网页测试家庭", base_currency="CNY")
        self.user = get_user_model().objects.create_user(username="global-web-user")
        self.member = FamilyMember.objects.create(
            family=self.family,
            user=self.user,
            display_name="网页成员",
            role=FamilyMember.ROLE_ADMIN,
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

    def test_only_admin_can_change_family_financial_authorization(self):
        url = reverse("ai_analysis:family_financial_authorization_update")
        response = self.client.post(url, {"is_allowed": "on"})
        self.assertRedirects(response, reverse("ai_analysis:index"))
        authorization = AiFamilyOutboundAuthorization.objects.get(
            family=self.family, provider=self.provider
        )
        self.assertTrue(authorization.is_allowed)
        self.assertEqual(authorization.changed_by, self.member)

        ordinary_user = get_user_model().objects.create_user(username="global-family-member")
        ordinary = FamilyMember.objects.create(
            family=self.family,
            user=ordinary_user,
            display_name="普通成员",
            role=FamilyMember.ROLE_MEMBER,
        )
        self.client.force_login(ordinary_user)
        denied = self.client.post(url, {})
        self.assertEqual(denied.status_code, 403)
        authorization.refresh_from_db()
        self.assertTrue(authorization.is_allowed)
        self.assertEqual(authorization.changed_by, self.member)
        status_page = self.client.get(reverse("ai_analysis:index"))
        self.assertContains(status_page, "当前状态：")
        self.assertContains(status_page, "已允许")
        self.assertContains(status_page, "只有家庭管理员可以修改此设置")
        self.assertNotContains(status_page, "保存家庭授权")

    @patch("ai_analysis.views.provider_configuration")
    def test_family_conversation_uses_family_authorization_without_personal_financial_grant(
        self, configuration
    ):
        configuration.return_value = (self.provider, {"fingerprint": "config-1"})
        self.conversation.financial_scope = AiConversation.SCOPE_FAMILY
        self.conversation.save(update_fields=["financial_scope", "updated_at"])
        url = reverse("ai_analysis:conversation", args=[self.conversation.pk])

        blocked = self.client.get(url)
        self.assertContains(blocked, "全家财务尚未获得家庭授权")
        self.assertContains(blocked, "需要家庭管理员先在右侧允许")

        AiFamilyOutboundAuthorization.objects.create(
            family=self.family,
            provider=self.provider,
            changed_by=self.member,
            is_allowed=True,
        )
        ready = self.client.get(url)
        self.assertContains(ready, "AI 助手已准备好")
        self.assertContains(ready, "发送")

    def test_pending_request_page_refreshes_until_completion(self):
        AiAnalysisRequest.objects.create(
            family=self.family,
            member=self.member,
            conversation=self.conversation,
            provider=self.provider,
            module="global_ai",
            analysis_type="chat_v1",
            prompt="请读取账本快照",
            status=AiAnalysisRequest.STATUS_PENDING,
        )
        response = self.client.get(
            reverse("ai_analysis:conversation", args=[self.conversation.pk])
        )
        self.assertContains(response, "window.location.reload")
        self.assertContains(response, "3000")
