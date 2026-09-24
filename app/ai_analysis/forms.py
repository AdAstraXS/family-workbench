from django import forms
from django.core.exceptions import ValidationError

from .models import AiModuleModel, AiProvider


class AiModuleModelForm(forms.ModelForm):
    class Meta:
        model = AiModuleModel
        fields = ("module", "provider")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["provider"].queryset = AiProvider.objects.filter(
            is_active=True, provider_type__in=("openai", "openai_compatible"),
        ).exclude(model_name__in=("", "待配置")).order_by("name", "model_name")
        self.fields["provider"].help_text = "只能选择已启用、已配置本模块数据与费用策略的文字模型；修改只影响新任务。"

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
