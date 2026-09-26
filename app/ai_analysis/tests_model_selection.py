from unittest.mock import patch

from django.apps import apps
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from ai_analysis.forms import AiModuleModelForm
from ai_analysis.model_selection import default_provider
from ai_analysis.models import AiModuleModel, AiProvider
from intelligence.ai_enrichment import resolve_text_ai_provider
from option_wheel.advice_jobs import provider_configuration


class ModuleModelSelectionTests(TestCase):
    def setUp(self):
        self.flash = AiProvider.objects.create(
            name="DeepSeek Flash", provider_type="openai_compatible",
            base_url="https://api.deepseek.com", model_name="deepseek-flash",
            extra_data={
                "api_key_env_var": "TEST_FLASH_KEY", "intelligence_max_input_characters": 20000,
                "intelligence_max_output_tokens": 1800,
                "intelligence_input_usd_per_million": "0.30",
                "intelligence_output_usd_per_million": "1.20",
                "intelligence_max_estimated_usd": "0.01",
            },
        )
        self.backup = AiProvider.objects.create(
            name="GLM FlashX", provider_type="openai_compatible",
            base_url="https://open.bigmodel.cn/api/paas/v4", model_name="glm-5.3-flashx",
            extra_data={"usage": "text", "api_key_env_var": "TEST_GLM_KEY"},
        )

    def test_wheel_uses_selected_flash_and_rejects_disabled_setting(self):
        AiModuleModel.objects.create(module="option_wheel", provider=self.flash)
        with patch.dict("os.environ", {"TEST_FLASH_KEY": "unit-test-key"}):
            selected, config = provider_configuration()
        self.assertEqual(selected, self.flash)
        self.assertEqual(config["model"], "deepseek-flash")
        self.flash.is_active = False
        self.flash.save(update_fields=["is_active"])
        with patch.dict("os.environ", {"TEST_FLASH_KEY": "unit-test-key"}):
            with self.assertRaisesMessage(ValueError, "原 DeepSeek 配置已停用"):
                provider_configuration()

    def test_intelligence_default_is_used_without_overriding_explicit_choice(self):
        AiModuleModel.objects.create(module="intelligence", provider=self.flash)
        self.assertEqual(resolve_text_ai_provider(), self.flash)
        self.assertEqual(resolve_text_ai_provider(self.backup.pk), self.backup)

    def test_admin_rejects_vision_and_unsupported_wheel_model(self):
        vision = AiProvider.objects.create(
            name="Vision", provider_type="openai_compatible", model_name="vision-v1",
            extra_data={"usage": "vision"},
        )
        for provider in (vision, self.backup):
            form = AiModuleModelForm(data={"module": "option_wheel", "provider": provider.pk})
            self.assertFalse(form.is_valid())
            self.assertNotIn(provider.pk, list(form.fields["provider"].queryset.values_list("pk", flat=True)))

    def test_admin_has_separate_module_defaults_page(self):
        url = reverse("admin:ai_analysis_aimodulemodel_changelist")
        user = get_user_model().objects.create_superuser("model-admin", "admin@example.test", "test")
        self.client.force_login(user)
        self.assertEqual(self.client.get(url).status_code, 200)
        setting = AiModuleModel.objects.create(module="option_wheel", provider=self.flash)
        response = self.client.get(url)
        self.assertContains(response, "期权分析建议")
        self.assertEqual(default_provider("option_wheel"), self.flash)
        edit = self.client.get(reverse("admin:ai_analysis_aimodulemodel_change", args=[setting.pk]))
        self.assertContains(edit, "DeepSeek Flash")
        self.assertNotContains(edit, "GLM FlashX")
        self.assertNotContains(edit, "Vision")

    def test_upgrade_preserves_key_reference_and_seeds_each_eligible_module(self):
        from importlib import import_module

        text_provider = AiProvider.objects.create(
            name="DeepSeek 文本整理", provider_type="openai_compatible",
            base_url="https://api.deepseek.com", model_name="deepseek-v4-flash",
            extra_data={"api_key_env_var": "EXISTING_DEEPSEEK_KEY", "custom_note": "keep"},
        )
        AiProvider.objects.create(
            name="DeepSeek 全局 AI", provider_type="openai_compatible",
            base_url="https://api.deepseek.com", model_name="deepseek-v4-pro",
            extra_data={
                "allow_research_analysis": True,
                "research_policy_version": "research-document-v1",
                "research_policy_reviewed_on": "2026-09-24",
                "research_max_input_chars": 20000,
                "research_max_output_tokens": 2000,
                "research_max_estimated_usd": "0.10",
            },
        )
        migration = import_module("ai_analysis.migrations.0011_module_model_defaults")
        migration.configure_defaults(apps, None)
        text_provider.refresh_from_db()
        self.assertEqual(text_provider.model_name, "deepseek-flash")
        self.assertEqual(text_provider.extra_data["api_key_env_var"], "EXISTING_DEEPSEEK_KEY")
        self.assertEqual(text_provider.extra_data["custom_note"], "keep")
        self.assertEqual(text_provider.extra_data["research_input_usd_per_million"], "0.30")
        self.assertEqual(AiModuleModel.objects.count(), 5)
        self.assertEqual(default_provider("investment_research"), text_provider)
        self.assertTrue(AiProvider.objects.filter(model_name="glm-5.3-flashx").exists())
