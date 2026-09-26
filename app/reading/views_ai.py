from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST
from . import ai
from .models import Annotation, ReadingAiJob
from .permissions import accessible_books
from .text import chapter_text, pdf_text
from .views import book_for, member_required


class ScopeForm(forms.Form):
    provider=forms.ChoiceField(label="AI 服务商和模型")
    kind=forms.ChoiceField(label="总结范围",choices=[("chapter","选定章节或 PDF 页段"),("notes","选定个人批注")])
    section=forms.IntegerField(label="章节序号（从 1 开始）",min_value=1,required=False)
    first=forms.IntegerField(label="PDF 起始页",min_value=1,required=False)
    last=forms.IntegerField(label="PDF 结束页（最多 20 页）",min_value=1,required=False)
    excerpt=forms.CharField(label="只总结本章/本页中的小节（可选，粘贴完整原文）",max_length=12000,required=False,widget=forms.Textarea(attrs={"rows":4}))
    notes=forms.MultipleChoiceField(label="我的批注",required=False,widget=forms.CheckboxSelectMultiple)

    def __init__(self,*args,book,member,**kwargs):
        super().__init__(*args,**kwargs)
        self.fields["provider"].choices=[(p.pk,f"{p.name} · {p.model_name}") for p in ai.providers()]
        self.fields["notes"].choices=[(str(n.pk),f"{n.quote[:70]} · {n.note[:60] or '仅划线'}") for n in Annotation.objects.filter(book=book,author=member)]
        self.book=book
        if book.file.format=="pdf":self.fields.pop("section")
        else:self.fields.pop("first");self.fields.pop("last")

    def clean(self):
        values=super().clean()
        if values.get("kind")=="chapter":
            if self.book.file.format=="pdf":
                first,last=values.get("first"),values.get("last")
                if first is None or last is None or not first<=last<first+20 or (self.book.file.page_count and last>self.book.file.page_count):
                    raise ValidationError("请选择文件范围内连续的 1 至 20 页。")
            elif values.get("section") is None or values["section"]>len(self.book.file.sections):raise ValidationError("请选择有效章节序号。")
            else:values["section"]-=1
        elif not values.get("notes"):raise ValidationError("请选择至少一条自己的批注。")
        return values


@member_required
@require_http_methods(["GET","POST"])
def create(request,pk):
    book=book_for(request,pk)
    initial={"section":request.GET.get("section",1),"first":request.GET.get("page",1),"last":request.GET.get("page",1)}
    form=ScopeForm(request.POST or None,book=book,member=request.reader_member,initial=initial)
    preview="";preview_title=""
    if request.method=="POST" and form.is_valid():
        try:
            if request.POST.get("action")=="preview" and form.cleaned_data["kind"]=="chapter":
                if book.file.format=="pdf":
                    pages=pdf_text(book.file,form.cleaned_data["first"],form.cleaned_data["last"])["pages"]
                    preview="\n\n".join(p["text"] for p in pages)
                else:preview=chapter_text(book.file,form.cleaned_data["section"])["text"]
                preview_title="所选原文（选取小节时可复制此处文字）"
            else:
                provider=next(p for p in ai.providers() if str(p.pk)==form.cleaned_data["provider"])
                job=ai.draft(book,request.reader_member,provider,form.cleaned_data)
                return redirect("reading:ai_job",job_id=job.pk)
        except ValidationError as exc:form.add_error(None,exc)
        except StopIteration:form.add_error("provider","服务商已停用，请刷新后重选。")
    return render(request,"reading/ai_create.html",{"book":book,"form":form,"preview":preview,"preview_title":preview_title,
                  "section_count":len(book.file.sections)})


def job_for(request,job_id):
    return get_object_or_404(ReadingAiJob.objects.filter(member=request.reader_member,book__in=accessible_books(request.reader_member)).select_related("book","provider"),pk=job_id)


@member_required
@require_http_methods(["GET","POST"])
def detail(request,job_id):
    job=job_for(request,job_id)
    if request.method=="POST":
        action=request.POST.get("action")
        try:
            if action=="confirm":
                ai.confirm(job);messages.success(request,"范围已确认，任务已排队。")
            elif action=="cancel":
                ReadingAiJob.objects.filter(pk=job.pk,status__in=["draft","queued","running"]).update(status="cancelled",finished_at=timezone.now())
                messages.info(request,"任务已取消。若请求已经发出，服务商仍可能计费；返回内容不会发布。")
            elif action=="publish":
                artifact=ai.publish_job(job)
                return redirect("reading:artifact",artifact_id=artifact.pk)
        except ValidationError as exc:messages.error(request,exc.messages[0])
        return redirect("reading:ai_job",job_id=job.pk)
    return render(request,"reading/ai_job.html",{"job":job})
