import re
from pathlib import Path

from django import forms

from .models import (
    KnowledgeArtifact,
    KnowledgeCategory,
    KnowledgeDocument,
    KnowledgeTag,
    KnowledgeVisibility,
    normalize_taxonomy_name,
)
from .taxonomy import canonicalize_document_taxonomy


class NotebookSelectionForm(forms.Form):
    notebook_id = forms.ChoiceField(label="试点笔记本")
    visibility = forms.ChoiceField(
        label="同步后的默认可见范围",
        choices=KnowledgeVisibility.choices,
        initial=KnowledgeVisibility.FAMILY,
    )
    allow_cloud_ai = forms.BooleanField(
        label="允许把这个笔记本的正文发送给已配置的云端 AI 进行整理",
        required=False,
        help_text="不勾选也可以同步、浏览和搜索，之后可再开启。",
    )

    def __init__(self, *args, notebooks=None, **kwargs):
        super().__init__(*args, **kwargs)
        notebooks = notebooks or []
        self.notebooks = {
            str(item.get("id", "")): item
            for item in notebooks
            if item.get("id") and item.get("displayName")
        }
        self.fields["notebook_id"].choices = [
            (notebook_id, item["displayName"])
            for notebook_id, item in self.notebooks.items()
        ]
        for field in self.fields.values():
            field.widget.attrs.setdefault("class", "form-control")

    def clean_notebook_id(self):
        notebook_id = self.cleaned_data["notebook_id"]
        if notebook_id not in self.notebooks:
            raise forms.ValidationError("所选笔记本已不在当前账户的可选列表中，请刷新后重试。")
        return notebook_id

    @property
    def selected_notebook(self):
        notebook_id = self.cleaned_data.get("notebook_id")
        return self.notebooks.get(notebook_id, {})


class KnowledgeImportUploadForm(forms.Form):
    source_name = forms.CharField(
        label="来源名称",
        max_length=300,
        initial="微信公众号归档",
        help_text="建议按公众号分别建立来源，例如“微信公众号 · 金渐成”。",
    )
    person_name = forms.CharField(
        label="归属人物",
        max_length=300,
        required=False,
        help_text="同一人的不同笔名或账号统一填写同一个人物名；例如“金渐成”。留空时沿用同来源最近确认的人物，原始署名仍会保留。",
    )
    category = forms.CharField(
        label="批次默认分类",
        max_length=100,
        initial="公众号归档",
        required=False,
        help_text="原目录会独立保留，不会被这个分类覆盖。",
    )
    visibility = forms.ChoiceField(
        label="导入后的可见范围",
        choices=KnowledgeVisibility.choices,
        initial=KnowledgeVisibility.FAMILY,
    )
    package = forms.FileField(
        label="HTML 或 ZIP 导入包",
        help_text="支持单个 .html 或包含 HTML、图片和封面的 .zip；上传后只检查，不会立即入库。",
        widget=forms.ClearableFileInput(attrs={"accept": ".html,.zip"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.setdefault("class", "form-control")

    def clean_package(self):
        uploaded = self.cleaned_data["package"]
        suffix = Path(uploaded.name).suffix.lower()
        if suffix not in {".html", ".zip"}:
            raise forms.ValidationError("目前只支持 .html 或 .zip 导入包。")
        return uploaded


class KnowledgeArtifactUploadForm(forms.Form):
    person_name = forms.CharField(
        label="关联人物",
        max_length=300,
        help_text="成果会显示在这个人物的历史知识主页。",
    )
    artifact_type = forms.ChoiceField(
        label="成果类型",
        choices=KnowledgeArtifact.TYPE_CHOICES,
    )
    title = forms.CharField(
        label="成果标题",
        max_length=500,
        required=False,
        help_text="留空时使用 HTML 的页面标题。",
    )
    description = forms.CharField(
        label="成果说明",
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    visibility = forms.ChoiceField(
        label="可见范围",
        choices=KnowledgeVisibility.choices,
        initial=KnowledgeVisibility.FAMILY,
    )
    source_article_count = forms.IntegerField(
        label="生成时使用的文章数",
        min_value=0,
        initial=439,
        help_text="这里记录 AI 实际使用的来源规模；当前已导入 437 篇不影响先建立映射。",
    )
    source_cutoff_date = forms.DateField(
        label="来源截止日期",
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    generator_name = forms.CharField(
        label="生成工具",
        max_length=100,
        required=False,
        initial="Claude",
    )
    generated_at = forms.DateTimeField(
        label="生成时间",
        required=False,
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
        help_text="不知道准确时间时可留空，页面会明确显示未记录。",
    )
    model_name = forms.CharField(
        label="模型",
        max_length=200,
        required=False,
        help_text="不知道具体模型时可留空，不进行猜测。",
    )
    prompt_version = forms.CharField(
        label="提示词版本",
        max_length=100,
        required=False,
        initial="external-ai-synthesis-v1",
    )
    html_file = forms.FileField(
        label="AI 生成的 HTML 文件",
        help_text="仅支持自包含 HTML，最大 5 MB；原文件会按版本保存在 NAS。",
        widget=forms.ClearableFileInput(attrs={"accept": ".html,.htm,text/html"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.setdefault("class", "form-control")

    def clean_person_name(self):
        value = " ".join(self.cleaned_data["person_name"].strip().split())
        if not value:
            raise forms.ValidationError("请填写关联人物。")
        return value

    def clean_html_file(self):
        uploaded = self.cleaned_data["html_file"]
        if Path(uploaded.name).suffix.lower() not in {".html", ".htm"}:
            raise forms.ValidationError("只支持 .html 或 .htm 文件。")
        if uploaded.size > 5 * 1024 * 1024:
            raise forms.ValidationError("HTML 文件不能超过 5 MB。")
        prefix = uploaded.read(4096)
        uploaded.seek(0)
        if b"\x00" in prefix or b"<html" not in prefix.lower():
            raise forms.ValidationError("文件不是可识别的 HTML 文档。")
        return uploaded


class DocumentOrganizeForm(forms.ModelForm):
    category = forms.CharField(
        label="分类",
        required=False,
        max_length=100,
        help_text="优先选择家庭已有分类；手工填写新分类时，保存后会加入分类目录。",
        widget=forms.TextInput(attrs={"list": "knowledge-category-options"}),
    )
    tags_text = forms.CharField(
        label="标签",
        required=False,
        help_text="优先使用下方已有标签；多个标签使用逗号、中文逗号或顿号分隔，最多 20 个。",
        widget=forms.Textarea(attrs={"rows": 2}),
    )

    class Meta:
        model = KnowledgeDocument
        fields = [
            "confirmed_summary",
            "category",
            "tags_text",
            "visibility",
        ]
        labels = {
            "confirmed_summary": "正式摘要",
            "category": "分类",
            "visibility": "可见范围",
        }
        widgets = {
            "confirmed_summary": forms.Textarea(attrs={"rows": 8}),
        }

    def __init__(self, *args, family=None, created_by=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.family = family or getattr(self.instance, "family", None)
        self.created_by = created_by
        categories = KnowledgeCategory.objects.none()
        tags = KnowledgeTag.objects.none()
        if self.family and getattr(self.family, "pk", None):
            categories = KnowledgeCategory.objects.filter(
                family=self.family,
                is_active=True,
                merged_into__isnull=True,
            ).order_by("name", "id")
            tags = KnowledgeTag.objects.filter(
                family=self.family,
                is_active=True,
                merged_into__isnull=True,
            ).order_by("name", "id")
        self.existing_categories = list(categories)
        self.existing_tags = list(tags)
        if self.instance and self.instance.pk:
            self.fields["tags_text"].initial = "，".join(self.instance.tags or [])
        for field in self.fields.values():
            field.widget.attrs.setdefault("class", "form-control")

    def clean_tags_text(self):
        values = re.split(r"[,，、\n]+", self.cleaned_data["tags_text"])
        tags = []
        for value in values:
            tag = value.strip()
            if tag and tag not in tags:
                tags.append(tag)
        if len(tags) > 20:
            raise forms.ValidationError("标签最多 20 个。")
        if any(len(tag) > 30 for tag in tags):
            raise forms.ValidationError("每个标签不能超过 30 个字符。")
        return tags

    def clean(self):
        cleaned_data = super().clean()
        if not any(
            [
                str(cleaned_data.get("confirmed_summary") or "").strip(),
                str(cleaned_data.get("category") or "").strip(),
                cleaned_data.get("tags_text"),
            ]
        ):
            raise forms.ValidationError("摘要、分类和标签至少需要完成一项。")
        return cleaned_data

    def save(self, commit=True):
        document = super().save(commit=False)
        document.tags = self.cleaned_data["tags_text"]
        canonicalize_document_taxonomy(document, created_by=self.created_by)
        if commit:
            document.save()
        return document


class _TaxonomyForm(forms.ModelForm):
    def __init__(self, *args, family=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.family = family or getattr(self.instance, "family", None)
        for field in self.fields.values():
            field.widget.attrs.setdefault("class", "form-control")

    def clean_name(self):
        name = " ".join(self.cleaned_data["name"].strip().split())
        model = self._meta.model
        queryset = model.objects.filter(
            family=self.family,
            normalized_name=normalize_taxonomy_name(name),
        )
        if self.instance.pk:
            queryset = queryset.exclude(pk=self.instance.pk)
        if queryset.exists():
            raise forms.ValidationError("家庭中已经存在同名项目，请直接使用或执行合并。")
        return name


class KnowledgeCategoryForm(_TaxonomyForm):
    class Meta:
        model = KnowledgeCategory
        fields = ["name", "description", "is_active"]
        labels = {
            "name": "分类名称",
            "description": "分类说明",
            "is_active": "允许继续用于整理",
        }


class KnowledgeTagForm(_TaxonomyForm):
    class Meta:
        model = KnowledgeTag
        fields = ["name", "description", "is_active"]
        labels = {
            "name": "标签名称",
            "description": "标签说明",
            "is_active": "允许继续用于整理",
        }


class TaxonomyMergeForm(forms.Form):
    target = forms.ModelChoiceField(label="合并到", queryset=KnowledgeCategory.objects.none())

    def __init__(self, *args, item=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.item = item
        if item:
            self.fields["target"].queryset = item.__class__.objects.filter(
                family=item.family,
                is_active=True,
                merged_into__isnull=True,
            ).exclude(pk=item.pk).order_by("name", "id")
        self.fields["target"].widget.attrs.setdefault("class", "form-control")


class ProposalReviewForm(forms.Form):
    ACTION_ACCEPT = "accept"
    ACTION_REJECT = "reject"
    ACTION_CHOICES = [
        (ACTION_ACCEPT, "接受"),
        (ACTION_REJECT, "拒绝"),
    ]

    action = forms.ChoiceField(label="处理方式", choices=ACTION_CHOICES)
    value = forms.CharField(
        label="确认内容",
        required=False,
        widget=forms.Textarea(attrs={"rows": 5, "class": "form-control"}),
    )

    def __init__(self, *args, proposal=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.proposal = proposal
        if proposal and not self.is_bound:
            suggested = proposal.suggested_value or {}
            if proposal.proposal_type == proposal.TYPE_TAGS:
                self.fields["value"].initial = "，".join(suggested.get("items", []))
            else:
                self.fields["value"].initial = suggested.get(
                    "text",
                    suggested.get("value", ""),
                )

    def clean_value(self):
        value = self.cleaned_data["value"].strip()
        if self.cleaned_data.get("action") == self.ACTION_ACCEPT and not value:
            raise forms.ValidationError("接受建议时确认内容不能为空。")
        return value


class BulkProposalPreviewForm(forms.Form):
    proposal_ids = forms.CharField(widget=forms.HiddenInput)

    def clean_proposal_ids(self):
        raw = self.cleaned_data["proposal_ids"]
        ids = []
        for value in raw.split(","):
            value = value.strip()
            if value.isdigit() and int(value) not in ids:
                ids.append(int(value))
        if not ids:
            raise forms.ValidationError("没有可处理的建议。")
        if len(ids) > 100:
            raise forms.ValidationError("一次最多批量确认 100 项建议。")
        return ids
