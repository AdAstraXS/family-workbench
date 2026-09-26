import json
import re
import zipfile
from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Prefetch, Q
from django.http import Http404, HttpResponse, HttpResponseForbidden, JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import content_disposition_header
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from family_core.permissions import current_member
from .forms import BookEditForm, BookUploadForm
from .models import Book, ReadingPosition
from .permissions import accessible_books
from .services import DuplicateBook, position_payload, retry_file, save_position, upload_book
from .storage import max_upload_bytes, storage


def member_required(fn):
    @login_required
    @wraps(fn)
    def wrapped(request, *args, **kwargs):
        request.reader_member = current_member(request)
        if request.reader_member is None:
            return HttpResponseForbidden("当前账户尚未绑定有效家庭成员。")
        response = fn(request, *args, **kwargs)
        response["Cache-Control"] = "private, no-store"
        response["Vary"] = "Cookie"
        return response
    return wrapped


def book_for(request, pk, lock=False):
    queryset = accessible_books(request.reader_member)
    if lock:
        queryset = queryset.select_for_update(of=("self",))
    return get_object_or_404(queryset, pk=pk)


@member_required
@require_GET
def index(request):
    books = accessible_books(request.reader_member)
    query = request.GET.get("q", "").strip()[:200]
    scope = request.GET.get("scope", "all")
    state = request.GET.get("state", "all")
    if query:
        books = books.filter(Q(title__icontains=query) | Q(author__icontains=query))
    if scope == "mine":
        books = books.filter(owner=request.reader_member)
    elif scope == "family":
        books = books.filter(visibility=Book.FAMILY)
    own_positions = ReadingPosition.objects.filter(member=request.reader_member)
    if state == "reading":
        books = books.filter(pk__in=own_positions.filter(completed_at__isnull=True).values("book_id"))
    elif state == "done":
        books = books.filter(pk__in=own_positions.filter(completed_at__isnull=False).values("book_id"))
    books = books.prefetch_related(Prefetch("positions", queryset=own_positions, to_attr="my_positions"))
    recent = own_positions.filter(book__in=accessible_books(request.reader_member), book__file__status="ready",
                                  completed_at__isnull=True).select_related("book").order_by("-updated_at")[:3]
    return render(request, "reading/shelf.html", {"page": Paginator(books, 24).get_page(request.GET.get("page")),
                  "query": query, "scope": scope, "state": state, "recent": recent})


@member_required
@require_http_methods(["GET", "POST"])
def upload(request):
    form = BookUploadForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        try:
            book = upload_book(request.reader_member, form.cleaned_data)
        except DuplicateBook as exc:
            messages.info(request, "你已经上传过这个文件，已打开原有图书。")
            return redirect("reading:detail", pk=exc.book.pk)
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            messages.success(request, "文件已保存，正在等待解析。处理结果会显示在图书详情。")
            return redirect("reading:detail", pk=book.pk)
    return render(request, "reading/upload.html", {"form": form, "max_mb": max_upload_bytes() // (1024 * 1024)})


@member_required
@require_GET
def detail(request, pk):
    from .annotations import accessible_annotations
    from .permissions import accessible_reading_artifacts
    book = book_for(request, pk)
    position = ReadingPosition.objects.filter(book=book, member=request.reader_member).first()
    return render(request, "reading/detail.html", {"book": book, "position": position,
                  "annotations": accessible_annotations(request.reader_member, book),
                  "artifacts": accessible_reading_artifacts(request.reader_member).filter(book=book),
                  "ai_jobs": book.ai_jobs.filter(member=request.reader_member)[:10],
                  "is_owner": book.owner_id == request.reader_member.pk,
                  "runs": book.file.runs.all()[:5] if book.owner_id == request.reader_member.pk else []})


@member_required
@require_http_methods(["GET", "POST"])
def edit(request, pk):
    with transaction.atomic():
        book = book_for(request, pk, lock=request.method == "POST")
        if book.owner_id != request.reader_member.pk:
            raise Http404
        form = BookEditForm(request.POST or None, instance=book)
        if request.method == "POST" and form.is_valid():
            form.save()
            messages.success(request, "图书信息已更新。阅读位置仍只对本人可见。")
            return redirect("reading:detail", pk=book.pk)
    return render(request, "reading/edit.html", {"form": form, "book": book})


@member_required
@require_POST
def retry(request, pk):
    book = book_for(request, pk)
    if book.owner_id != request.reader_member.pk:
        raise Http404
    try:
        retry_file(book)
        messages.success(request, "已重新排队。请稍后刷新处理结果。")
    except ValidationError as exc:
        messages.error(request, exc.messages[0])
    return redirect("reading:detail", pk=pk)


@member_required
@require_POST
def completion(request, pk):
    with transaction.atomic():
        book = book_for(request, pk, lock=True)
        position = ReadingPosition.objects.filter(book=book, member=request.reader_member).first()
        if position is None:
            messages.error(request, "请先开始阅读，保存位置后再确认读完。")
        else:
            done = request.POST.get("completed") == "yes"
            ReadingPosition.objects.filter(pk=position.pk).update(completed_at=timezone.now() if done else None)
            messages.success(request, "已由你确认读完。" if done else "已恢复为在读。")
    return redirect("reading:detail", pk=pk)


@member_required
@require_GET
def reader(request, pk):
    book = book_for(request, pk)
    if book.file.status != "ready":
        return redirect("reading:detail", pk=pk)
    data = {"format": book.file.format, "manifest": reverse("reading:manifest", args=[pk]),
            "annotations": reverse("reading:annotations", args=[pk]),
            "position": reverse("reading:position", args=[pk]), "file": reverse("reading:file", args=[pk]),
            "file_hash": book.file.sha256, "normalizer_version": book.file.normalizer_version,
            "writable": request.reader_member.role != "viewer"}
    response = render(request, "reading/reader.html", {"book": book, "reader_config": data})
    response["Content-Security-Policy"] = (
        "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src blob: data:; font-src 'self' blob: data:; frame-src blob:; "
        "connect-src 'self'; worker-src 'self' blob:; object-src 'none'; base-uri 'none'; "
        "form-action 'self'; frame-ancestors 'self'")
    return response


@member_required
@require_GET
def manifest(request, pk):
    book = book_for(request, pk)
    if book.file.status != "ready" or book.file.format not in {"epub", "txt"}:
        raise Http404
    return JsonResponse({"resources": book.file.resources, "sections": book.file.sections,
                         "resource_url": reverse("reading:resource", args=[pk])})


@member_required
@require_GET
def resource(request, pk):
    book = book_for(request, pk)
    file = book.file
    path = request.GET.get("path", "")
    if file.status != "ready" or not file.normalized_path or path not in file.resources:
        raise Http404
    try:
        with zipfile.ZipFile(storage().path(file.normalized_path)) as archive:
            data = archive.read(path)
    except (OSError, KeyError, zipfile.BadZipFile) as exc:
        raise Http404("阅读内容暂不可用，请联系上传者。") from exc
    response = HttpResponse(data, content_type="application/octet-stream")
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = "sandbox; default-src 'none'; frame-ancestors 'none'"
    return response


@member_required
@require_http_methods(["GET", "HEAD"])
def file(request, pk):
    book = book_for(request, pk)
    file = book.file
    try:
        stream = storage().open(file.original_path, "rb")
    except OSError as exc:
        raise Http404("原件暂不可用。") from exc
    size = file.size
    start, end, status = 0, size - 1, 200
    etag = f'"{file.sha256}"'
    value = request.headers.get("Range")
    if value and request.headers.get("If-Range", etag) == etag:
        match = re.fullmatch(r"bytes=(\d{0,20})-(\d{0,20})", value)
        valid = match and any(match.groups())
        if valid:
            a, b = match.groups()
            if a:
                start, end = int(a), min(int(b), end) if b else end
            else:
                start = max(0, size - int(b))
            valid = 0 <= start <= end < size
        if not valid:
            stream.close()
            response = HttpResponse(status=416)
            response["Content-Range"] = f"bytes */{size}"
            return response
        status = 206
    if request.method == "HEAD":
        stream.close()
        response = HttpResponse(status=status, content_type="application/octet-stream")
    else:
        def chunks():
            try:
                stream.seek(start)
                remaining = end - start + 1
                while remaining:
                    block = stream.read(min(65536, remaining))
                    if not block:
                        break
                    remaining -= len(block)
                    yield block
            finally:
                stream.close()
        response = StreamingHttpResponse(chunks(), status=status, content_type="application/octet-stream")
        response._resource_closers.append(stream.close)
    response["Content-Length"] = str(end - start + 1)
    response["Accept-Ranges"] = "bytes"
    response["ETag"] = etag
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = "sandbox; default-src 'none'; frame-ancestors 'none'"
    response["Content-Disposition"] = content_disposition_header(True, file.original_name)
    if status == 206:
        response["Content-Range"] = f"bytes {start}-{end}/{size}"
    return response


@member_required
@require_http_methods(["GET", "POST"])
def position(request, pk):
    if request.method == "GET":
        book = book_for(request, pk)
        return JsonResponse(position_payload(ReadingPosition.objects.filter(book=book, member=request.reader_member).first()))
    if len(request.body) > 12000:
        return JsonResponse({"error": "请求过大。"}, status=400)
    try:
        data = json.loads(request.body)
        if not isinstance(data, dict):
            raise ValueError
        with transaction.atomic():
            book = book_for(request, pk, lock=True)
            if book.file.status != "ready":
                raise ValidationError("图书尚未可读。")
            obj, updated = save_position(book, request.reader_member, data)
    except (ValueError, UnicodeError):
        return JsonResponse({"error": "请求格式不正确。"}, status=400)
    except ValidationError as exc:
        return JsonResponse({"error": exc.messages[0]}, status=400)
    return JsonResponse(position_payload(obj), status=200 if updated else 409)
