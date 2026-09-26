import json
from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import content_disposition_header
from django.views.decorators.http import require_GET, require_http_methods, require_POST
from .artifacts import LABELS, archive_version, canonical, export_html, publish, read_upload, validate_data
from .models import Book, ReadingArtifact
from .permissions import accessible_reading_artifacts
from .storage import storage
from .views import book_for, member_required


class ArtifactUploadForm(forms.Form):
    title=forms.CharField(label="成果标题",max_length=250)
    visibility=forms.ChoiceField(label="可见范围",choices=Book.VISIBILITY)
    file=forms.FileField(label="成果 HTML",help_text="单 HTML，UTF-8，最大 5 MB；原件保留。含标准数据时可编辑结构。")


@member_required
@require_http_methods(["GET","POST"])
def upload(request,pk):
    book=book_for(request,pk)
    form=ArtifactUploadForm(request.POST or None,request.FILES or None)
    if request.method=="POST" and form.is_valid():
        try:
            raw,data=read_upload(form.cleaned_data["file"])
            version,created=publish(book,request.reader_member,form.cleaned_data["title"],form.cleaned_data["visibility"],raw,data,
                                    name=form.cleaned_data["file"].name)
            messages.success(request,"成果已保存。" if created else "这个文件你已上传过，已打开原有成果。")
            return redirect("reading:artifact",artifact_id=version.artifact_id)
        except ValidationError as exc:form.add_error(None,exc)
    return render(request,"reading/artifact_upload.html",{"book":book,"form":form})


def artifact_for(request,artifact_id):
    return get_object_or_404(accessible_reading_artifacts(request.reader_member),pk=artifact_id)


def display_data(version):
    data=version.data
    sources={s["id"]:dict(s) for s in data.get("sources",[])}
    for source in sources.values():
        source["url"]=""
        if source.get("verified"):
            loc=source.get("location",{})
            if source.get("note_id"):
                source["url"]=reverse("reading:note",args=[source["note_id"]])
            elif "page" in loc or "section" in loc:
                key="page" if "page" in loc else "section"
                source["url"]=reverse("reading:reader",args=[version.artifact.book_id])+f"?{key}={loc[key]}"
    sections=[{**s,"label":LABELS[s["kind"]],"sources":[sources[x] for x in s["source_ids"]]} for s in data.get("sections",[])]
    nodes={n["id"]:{**n,"children":[]} for n in data.get("nodes",[])};roots=[]
    for node in nodes.values():
        if node["parent"]:nodes[node["parent"]]["children"].append(node)
        else:roots.append(node)
    return sections,roots,list(sources.values())


@member_required
@require_GET
def detail(request,artifact_id,number=None):
    artifact=artifact_for(request,artifact_id)
    version=get_object_or_404(artifact.versions,number=number) if number else artifact.current_version
    sections,nodes,sources=display_data(version)
    response=render(request,"reading/artifact.html",{"artifact":artifact,"version":version,"sections":sections,"nodes":nodes,
                  "sources":sources,"owned":artifact.owner_id==request.reader_member.pk,"versions":artifact.versions.all()})
    response["Content-Security-Policy"]="frame-src 'self'; object-src 'none'; base-uri 'self'"
    return response


@member_required
@require_GET
def file(request,artifact_id,number):
    artifact=artifact_for(request,artifact_id);version=get_object_or_404(artifact.versions,number=number)
    try:
        with storage().open(version.original_path,"rb") as source:raw=source.read()
    except OSError:raise Http404
    # Active content runs only inside the controlled parent, whose frame-src also
    # blocks a hostile HTML file from navigating its own frame to an external URL.
    preview=request.GET.get("preview")=="1" and request.headers.get("Sec-Fetch-Dest")=="iframe"
    response=HttpResponse(raw,content_type="text/html; charset=utf-8" if preview else "application/octet-stream")
    response["Content-Security-Policy"]=("sandbox allow-scripts; default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
        "img-src data:; font-src data:; connect-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'self'")
    response["X-Content-Type-Options"]="nosniff"
    response["X-Frame-Options"]="SAMEORIGIN"
    if not preview:response["Content-Disposition"]=content_disposition_header(True,version.original_name)
    return response


@member_required
@require_http_methods(["GET","POST"])
def edit(request,artifact_id):
    artifact=artifact_for(request,artifact_id)
    if artifact.owner_id!=request.reader_member.pk:raise Http404
    current=artifact.current_version
    error="";value=canonical(current.data) if current.data else ""
    if request.method=="POST":
        try:
            if request.POST.get("mode")=="file":
                uploaded=request.FILES.get("file")
                if not uploaded:raise ValidationError("请选择新的成果 HTML 文件。")
                raw,data=read_upload(uploaded)
                title=request.POST.get("title",artifact.title).strip()
                if not title or len(title)>250:raise ValidationError("标题须为 1 至 250 字。")
                publish(artifact.book,request.reader_member,title,artifact.visibility,raw,data,artifact=artifact,
                        base_version=int(request.POST.get("base_version","0")),name=uploaded.name)
                return redirect("reading:artifact",artifact_id=artifact.pk)
            edited=json.loads(canonical(current.data))
            if request.POST.get("mode")=="fields" and edited:
                edited["title"]=request.POST.get("title","")
                for section in edited["sections"]:
                    section["title"]=request.POST.get(f'title-{section["id"]}',"")
                    section["content"]=request.POST.get(f'content-{section["id"]}',"")
                    section["kind"]=request.POST.get(f'kind-{section["id"]}',section["kind"])
                for node in edited["nodes"]:node["title"]=request.POST.get(f'node-{node["id"]}',"")
                value=canonical(edited)
            else:value=request.POST.get("data","")
            if len(value)>300000:raise ValidationError("成果结构过长。")
            # Preserve only existing, server-established source snapshots.
            original={s["id"]:s for s in current.data.get("sources",[])}
            data=validate_data(json.loads(value),trusted_sources=original if original else None)
            version,_=publish(artifact.book,request.reader_member,data["title"],artifact.visibility,export_html(data),data,
                              kind="manual",artifact=artifact,base_version=int(request.POST.get("base_version","0")))
            return redirect("reading:artifact",artifact_id=artifact.pk)
        except (ValueError,RecursionError):error="请提交完整 JSON 结构。"
        except ValidationError as exc:error=exc.messages[0]
    return render(request,"reading/artifact_edit.html",{"artifact":artifact,"value":value,"error":error,"base_version":current.pk,"data":current.data})


@member_required
@require_POST
def share(request,artifact_id):
    artifact=artifact_for(request,artifact_id)
    if artifact.owner_id!=request.reader_member.pk:raise Http404
    visibility=request.POST.get("visibility")
    if visibility in {Book.PRIVATE,Book.FAMILY}:
        artifact.visibility=visibility;artifact.save(update_fields=["visibility","updated_at"])
        # Existing publication keeps its sharing scope; revocation is also enforced dynamically.
        messages.success(request,"成果可见范围已更新；阅读权限仍以图书为准。")
    return redirect("reading:artifact",artifact_id=artifact.pk)


@member_required
@require_POST
def archive(request,artifact_id,number):
    artifact=artifact_for(request,artifact_id)
    if artifact.owner_id!=request.reader_member.pk:raise Http404
    version=get_object_or_404(artifact.versions,number=number)
    try:doc=archive_version(version,request.reader_member)
    except ValidationError as exc:
        messages.error(request,exc.messages[0]);return redirect("reading:artifact",artifact_id=artifact.pk)
    messages.success(request,"此发布版本已归档；后续编辑会产生新版本。")
    return redirect("knowledge:document_detail",pk=doc.pk)
