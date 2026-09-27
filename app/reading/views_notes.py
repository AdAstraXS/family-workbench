import json
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST
from .annotations import accessible_annotations, annotation_payload, same_passage, validate_annotation
from .models import Annotation, AnnotationComment, Book
from .text import compact_text
from .views import member_required, book_for


@member_required
@require_http_methods(["GET","POST"])
def annotations(request, pk):
    book = book_for(request,pk)
    if request.method == "GET":
        return JsonResponse({"items":[annotation_payload(x,request.reader_member) for x in accessible_annotations(request.reader_member,book)]})
    try:
        if len(request.body)>60000:
            raise ValidationError("批注内容过长。")
        data=json.loads(request.body)
        if not isinstance(data,dict) or book.file.status!="ready":
            raise ValidationError("图书尚未可读或请求无效。")
        values=validate_annotation(book,data)
        with transaction.atomic():
            book_for(request,pk,lock=True)  # Recheck permission after potentially slow PDF extraction.
            existing=[item for item in Annotation.objects.filter(book=book,author=request.reader_member)
                      if same_passage(item.anchor,values["anchor"])]
            if existing:
                item=max(existing,key=lambda x:len(compact_text(x.quote)))
                if values["note"] and item.note:
                    return JsonResponse({**annotation_payload(item,request.reader_member),"outcome":"existing_note"})
                longer=len(compact_text(values["quote"]))>len(compact_text(item.quote))
                note_added=bool(values["note"]) and not item.note
                if longer or note_added:
                    changes={"updated_at":timezone.now(),"revision":item.revision+1}
                    if longer:
                        changes.update(quote=values["quote"],anchor=values["anchor"],text_matched=values["text_matched"])
                    if note_added:
                        changes.update(note=values["note"],visibility=values["visibility"])
                    Annotation.objects.filter(pk=item.pk).update(**changes)
                    item.refresh_from_db()
                outcome="expanded" if longer else "annotated" if note_added else "unchanged"
                return JsonResponse({**annotation_payload(item,request.reader_member),"outcome":outcome})
            item=Annotation.objects.create(book=book,author=request.reader_member,**values)
        return JsonResponse({**annotation_payload(item,request.reader_member),"outcome":"created"},status=201)
    except (ValueError,UnicodeError):
        return JsonResponse({"error":"请求格式不正确。"},status=400)
    except ValidationError as exc:
        return JsonResponse({"error":exc.messages[0]},status=400)


@member_required
@require_http_methods(["DELETE"])
def highlight_visibility(request, note_id):
    try:
        if len(request.body)>1000:
            raise ValueError
        data=json.loads(request.body)
    except (ValueError, UnicodeError):
        return JsonResponse({"error":"请求格式不正确。"},status=400)
    if not isinstance(data,dict) or type(data.get("revision")) is not int:
        return JsonResponse({"error":"划线版本无效。"},status=400)
    with transaction.atomic():
        item=get_object_or_404(accessible_annotations(request.reader_member),pk=note_id)
        book_for(request,item.book_id,lock=True)
        if item.author_id!=request.reader_member.pk:
            raise Http404
        if item.revision!=data["revision"]:
            return JsonResponse({"error":"划线已在另一个页面更新，请刷新后重试。"},status=409)
        if item.note.strip() or item.comments.exists():
            return JsonResponse({"error":"有批注或回复的划线不能取消。"},status=400)
        item.delete()
    return JsonResponse({"deleted":True})


@member_required
@require_POST
def annotation_edit(request, note_id):
    try:
        if len(request.body)>30000:
            raise ValueError
        data=json.loads(request.body)
    except (ValueError, UnicodeError):
        return JsonResponse({"error":"请求格式不正确。"},status=400)
    if not isinstance(data,dict) or type(data.get("revision")) is not int:
        return JsonResponse({"error":"批注版本无效。"},status=400)
    note, visibility=data.get("note"),data.get("visibility")
    if not isinstance(note,str) or len(note)>10000 or visibility not in {Book.PRIVATE,Book.FAMILY}:
        return JsonResponse({"error":"批注长度或分享范围无效。"},status=400)
    with transaction.atomic():
        item=get_object_or_404(accessible_annotations(request.reader_member),pk=note_id)
        book_for(request,item.book_id,lock=True)
        if item.author_id!=request.reader_member.pk:
            raise Http404
        changed=Annotation.objects.filter(pk=item.pk,revision=data["revision"]).update(
            note=note.strip(),visibility=visibility,highlight_visible=True,
            revision=data["revision"]+1,updated_at=timezone.now())
        if not changed:
            return JsonResponse({"error":"批注已在另一个页面更新，请刷新后重试。"},status=409)
        item.refresh_from_db()
    return JsonResponse(annotation_payload(item,request.reader_member))


@member_required
@require_http_methods(["GET","POST"])
def note_detail(request, note_id):
    with transaction.atomic():
        item=get_object_or_404(accessible_annotations(request.reader_member),pk=note_id)
        if request.method=="POST":
            book_for(request,item.book_id,lock=True)
            if item.author_id!=request.reader_member.pk:
                raise Http404
            note=request.POST.get("note","").strip()
            visibility=request.POST.get("visibility",Book.PRIVATE)
            if len(note)>10000 or visibility not in {Book.PRIVATE,Book.FAMILY}:
                messages.error(request,"批注长度或分享范围无效。")
            else:
                revision=request.POST.get("revision","")
                if not revision.isdigit() or not Annotation.objects.filter(pk=item.pk,revision=int(revision)).update(
                    note=note,visibility=visibility,highlight_visible=bool(note) or item.highlight_visible,
                    revision=int(revision)+1,updated_at=timezone.now()):
                    messages.error(request,"批注已在另一个页面更新，请重新核对后保存。")
                else:
                    messages.success(request,"批注已保存。")
            return redirect("reading:note",note_id=item.pk)
    return render(request,"reading/note.html",{"note":item,"owned":item.author_id==request.reader_member.pk,
                "comments":item.comments.select_related("author")})


@member_required
@require_POST
def comment(request, note_id):
    with transaction.atomic():
        item=get_object_or_404(accessible_annotations(request.reader_member),pk=note_id)
        book_for(request,item.book_id,lock=True)
        item=get_object_or_404(accessible_annotations(request.reader_member),pk=note_id)
        body=request.POST.get("body","").strip()
        if not body or len(body)>5000:
            messages.error(request,"回复需为 1 至 5000 字。")
        else:
            if not item.highlight_visible:
                Annotation.objects.filter(pk=item.pk).update(highlight_visible=True,revision=item.revision+1,updated_at=timezone.now())
            AnnotationComment.objects.create(annotation=item,author=request.reader_member,body=body)
    return redirect("reading:note",note_id=item.pk)


@member_required
@require_POST
def comment_edit(request, comment_id):
    item=get_object_or_404(AnnotationComment.objects.filter(annotation__in=accessible_annotations(request.reader_member)),pk=comment_id,author=request.reader_member)
    body=request.POST.get("body","").strip()
    revision=request.POST.get("revision","")
    if not body or len(body)>5000 or not revision.isdigit():
        messages.error(request,"回复内容或版本不正确。")
    elif not AnnotationComment.objects.filter(pk=item.pk,revision=int(revision)).update(body=body,revision=int(revision)+1,updated_at=timezone.now()):
        messages.error(request,"回复已有更新，请刷新后核对。")
    else:
        messages.success(request,"你的回复已更新。")
    return redirect("reading:note",note_id=item.annotation_id)
