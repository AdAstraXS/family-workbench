import re

from django import forms

from .models import InvestmentNote


class InvestmentNoteForm(forms.ModelForm):
    tags_text = forms.CharField(
        label="标签",
        required=False,
        help_text="多个标签请用逗号、中文逗号或顿号分隔，最多 20 个。",
        widget=forms.TextInput(attrs={"placeholder": "例如：港股、复盘、风险控制"}),
    )

    class Meta:
        model = InvestmentNote
        fields = ["title", "note_type", "note_date", "visibility", "tags_text", "content"]
        widgets = {
            "title": forms.TextInput(attrs={"placeholder": "给这篇笔记起个标题"}),
            "note_date": forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
            "content": forms.Textarea(
                attrs={
                    "rows": 14,
                    "placeholder": "记录你的判断、依据、执行情况和复盘结论……",
                }
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.setdefault("class", "form-control")
        if self.instance and self.instance.pk:
            self.fields["tags_text"].initial = "，".join(self.instance.tags or [])

    def clean_tags_text(self):
        raw_tags = re.split(r"[,，、\n]+", self.cleaned_data["tags_text"])
        tags = []
        for raw_tag in raw_tags:
            tag = raw_tag.strip()
            if tag and tag not in tags:
                tags.append(tag)
        if len(tags) > 20:
            raise forms.ValidationError("标签最多填写 20 个。")
        if any(len(tag) > 30 for tag in tags):
            raise forms.ValidationError("每个标签不能超过 30 个字符。")
        return tags

    def save(self, commit=True):
        note = super().save(commit=False)
        note.tags = self.cleaned_data["tags_text"]
        if commit:
            note.save()
        return note
