from django import forms

from .models import AiConversation, AiMemory


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
