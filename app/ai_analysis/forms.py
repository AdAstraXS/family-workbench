from django import forms

from .models import AiConversation, AiMemory, AiOutboundAuthorization


class ConversationCreateForm(forms.Form):
    title = forms.CharField(
        label="对话名称",
        max_length=200,
        required=False,
        widget=forms.TextInput(attrs={"placeholder": "例如：回顾本月家庭资产"}),
    )
    financial_scope = forms.ChoiceField(
        label="财务范围",
        choices=AiConversation.SCOPE_CHOICES,
        initial=AiConversation.SCOPE_PERSONAL,
    )

    def __init__(self, *args, allow_family=True, **kwargs):
        super().__init__(*args, **kwargs)
        if not allow_family:
            self.fields["financial_scope"].choices = [
                (AiConversation.SCOPE_PERSONAL, "我的财务")
            ]


class MemoryCreateForm(forms.Form):
    content = forms.CharField(
        label="希望 AI 记住的内容",
        max_length=2000,
        widget=forms.Textarea(
            attrs={"rows": 3, "placeholder": "例如：我更关注长期资产配置，不希望根据短期波动频繁调整。"}
        ),
    )
    visibility = forms.ChoiceField(
        label="谁可以使用",
        choices=AiMemory.VISIBILITY_CHOICES,
        initial=AiMemory.VISIBILITY_PERSONAL,
    )

    def __init__(self, *args, allow_family=True, **kwargs):
        super().__init__(*args, **kwargs)
        if not allow_family:
            self.fields["visibility"].choices = [
                (AiMemory.VISIBILITY_PERSONAL, "仅自己")
            ]


class MemoryRevisionForm(forms.Form):
    content = forms.CharField(label="修改后的内容", max_length=2000)


class GlobalAiPromptForm(forms.Form):
    content = forms.CharField(
        label="问题",
        max_length=4000,
        widget=forms.Textarea(attrs={"rows": 3, "placeholder": "输入问题；需要资料时，AI 会调用已授权的只读工具。"}),
    )
    idempotency_key = forms.CharField(max_length=100, widget=forms.HiddenInput())


class OutboundAuthorizationForm(forms.Form):
    allowed_data_types = forms.MultipleChoiceField(
        label="允许发送给云端 AI 的资料",
        choices=AiOutboundAuthorization.DATA_TYPE_CHOICES,
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )


class FamilyFinancialAuthorizationForm(forms.Form):
    is_allowed = forms.BooleanField(
        label="允许家庭成员在“全家财务”对话中把家庭财务资料发送给当前云端模型",
        required=False,
    )
