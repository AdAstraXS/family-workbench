from pathlib import Path
from django import forms
from .models import Book
from .storage import max_upload_bytes


class BookUploadForm(forms.ModelForm):
    file = forms.FileField(label="电子书", help_text="EPUB、PDF、TXT、MOBI。MOBI 本阶段仅保留原件，阅读转换尚未接入。")

    class Meta:
        model = Book
        fields = ["title", "author", "visibility", "file"]

    def clean_file(self):
        value = self.cleaned_data["file"]
        if Path(value.name).suffix.lower() not in {".epub", ".pdf", ".txt", ".mobi"}:
            raise forms.ValidationError("请选择 EPUB、PDF、TXT 或 MOBI 文件。")
        if not value.size or value.size > max_upload_bytes():
            raise forms.ValidationError(f"文件大小须在 1 字节至 {max_upload_bytes() // (1024 * 1024)} MB 之间。")
        return value


class BookEditForm(forms.ModelForm):
    class Meta:
        model = Book
        fields = ["title", "author", "description", "visibility"]
