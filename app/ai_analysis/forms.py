from django import forms
from django.core.exceptions import ValidationError

from .models import AiConversation, AiMemory, AiModuleModel, AiOutboundAuthorization, AiProvider


class AiModuleModelForm(forms.ModelForm):
    class Meta:
        model = AiModuleModel
        fields = ("module", "provider")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        rows = AiProvider.objects.filter(
            is_active=True, provider_type__in=("openai", "openai_compatible"),
        ).exclude(model_name__in=("", "待配置")).order_by("name", "model_name")
        module = self.data.get("module") or getattr(self.instance, "module", None)
        eligible_ids = [
            provider.pk for provider in rows
            if (provider.extra_data or {}).get("usage") not in {"vision", "image", "ipo_image_recognition"}
            and (not module or self._supports_module(provider, module))
        ]
        self.fields["provider"].queryset = rows.filter(pk__in=eligible_ids)
        self.fields["provider"].help_text = "只能选择已启用、已配置本模块数据与费用策略的文字模型；修改只影响新任务。"

    @staticmethod
    def _supports_module(provider, module):
        if module == AiModuleModel.OPTION_WHEEL:
            return (provider.model_name in {"deepseek-flash", "deepseek-v4-flash"}
                    and provider.base_url.rstrip("/") in {"https://api.deepseek.com", "https://api.deepseek.com/v1"})
        if module == AiModuleModel.INVESTMENT_RESEARCH:
            from investment_research.research_ai import ResearchAiError, research_provider_policy
            try:
                research_provider_policy(provider)
                return True
            except ResearchAiError:
                return False
        if module == AiModuleModel.INTELLIGENCE:
            from intelligence.ai_enrichment import IntelligenceAiError, intelligence_provider_policy
            try:
                intelligence_provider_policy(provider)
                return True
            except IntelligenceAiError:
                return False
        if module == AiModuleModel.KNOWLEDGE:
            return (bool((provider.extra_data or {}).get("api_key_env_var"))
                    and provider.base_url.rstrip("/") in {"https://api.deepseek.com", "https://api.deepseek.com/v1"})
        return module == AiModuleModel.GLOBAL_AI

    def clean(self):
        values = super().clean()
        module, provider = values.get("module"), values.get("provider")
        if not module or not provider:
            return values
        if (provider.extra_data or {}).get("usage") in {"vision", "image", "ipo_image_recognition"}:
            raise ValidationError("图片识别模型不能作为文字分析模型。")
        if module == AiModuleModel.OPTION_WHEEL:
            if not (provider.model_name in {"deepseek-flash", "deepseek-v4-flash"}
                    and provider.base_url.rstrip("/") in {"https://api.deepseek.com", "https://api.deepseek.com/v1"}):
                raise ValidationError("期权建议目前只支持已验证的 DeepSeek Flash；其他模型还需完成逐合约校验。")
        elif module == AiModuleModel.INVESTMENT_RESEARCH:
            from investment_research.research_ai import ResearchAiError, research_provider_policy
            try:
                research_provider_policy(provider)
            except ResearchAiError as exc:
                raise ValidationError(str(exc)) from exc
        elif module == AiModuleModel.INTELLIGENCE:
            from intelligence.ai_enrichment import IntelligenceAiError, intelligence_provider_policy
            try:
                intelligence_provider_policy(provider)
            except IntelligenceAiError as exc:
                raise ValidationError(str(exc)) from exc
        elif module == AiModuleModel.KNOWLEDGE:
            if not (provider.extra_data or {}).get("api_key_env_var"):
                raise ValidationError("知识整理模型必须指定服务器环境变量中的 API Key。")
            if provider.base_url.rstrip("/") not in {"https://api.deepseek.com", "https://api.deepseek.com/v1"}:
                raise ValidationError("知识来源的现有云端授权不包含跨服务商切换；此处暂限 DeepSeek。")
        elif module != AiModuleModel.GLOBAL_AI:
            raise ValidationError("不支持的分析模块。")
        return values


class ConversationCreateForm(forms.Form):
    title = forms.CharField(label="对话名称", max_length=200, required=False,
                            widget=forms.TextInput(attrs={"placeholder": "例如：回顾本月家庭资产"}))
    financial_scope = forms.ChoiceField(label="财务范围", choices=AiConversation.SCOPE_CHOICES,
                                        initial=AiConversation.SCOPE_PERSONAL)

    def __init__(self, *args, allow_family=True, **kwargs):
        super().__init__(*args, **kwargs)
        if not allow_family:
            self.fields["financial_scope"].choices = [(AiConversation.SCOPE_PERSONAL, "我的财务")]


class ConversationRenameForm(forms.Form):
    title = forms.CharField(label="新标题", max_length=200, strip=True,
                            widget=forms.TextInput(attrs={"placeholder": "输入新的对话标题"}))


class MemoryCreateForm(forms.Form):
    content = forms.CharField(label="希望 AI 记住的内容", max_length=2000,
                              widget=forms.Textarea(attrs={"rows": 3, "placeholder": "例如：我更关注长期资产配置，不希望根据短期波动频繁调整。"}))
    visibility = forms.ChoiceField(label="谁可以使用", choices=AiMemory.VISIBILITY_CHOICES,
                                   initial=AiMemory.VISIBILITY_PERSONAL)

    def __init__(self, *args, allow_family=True, **kwargs):
        super().__init__(*args, **kwargs)
        if not allow_family:
            self.fields["visibility"].choices = [(AiMemory.VISIBILITY_PERSONAL, "仅自己")]


class MemoryRevisionForm(forms.Form):
    content = forms.CharField(label="修改后的内容", max_length=2000)


class GlobalAiPromptForm(forms.Form):
    content = forms.CharField(label="问题", widget=forms.Textarea(
        attrs={"rows": 3, "placeholder": "输入问题；需要资料时，AI 会调用已授权的只读工具。"}))
    idempotency_key = forms.CharField(max_length=100, widget=forms.HiddenInput())


class OutboundAuthorizationForm(forms.Form):
    allowed_data_types = forms.MultipleChoiceField(label="允许发送给云端 AI 的资料",
                                                    choices=AiOutboundAuthorization.DATA_TYPE_CHOICES,
                                                    required=False, widget=forms.CheckboxSelectMultiple)


class FamilyFinancialAuthorizationForm(forms.Form):
    is_allowed = forms.BooleanField(
        label="允许家庭成员在“全家财务”对话中把家庭财务资料发送给当前云端模型", required=False)
