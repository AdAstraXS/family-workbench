from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import Http404, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from family_core.models import FamilyMember

from .forms import InvestmentNoteForm
from .models import InvestmentNote, InvestmentNoteType


def _current_member(request):
    try:
        member = request.user.family_member
    except FamilyMember.DoesNotExist:
        return None
    return member if member.is_active else None


def _membership_required_response(request):
    return render(
        request,
        "notes/membership_required.html",
        status=403,
    )


def _accessible_notes(member):
    return InvestmentNote.objects.filter(family=member.family).filter(
        Q(member=member) | Q(visibility=InvestmentNote.VISIBILITY_FAMILY)
    ).select_related("member", "note_type")


def _accessible_note_or_404(member, pk):
    return get_object_or_404(_accessible_notes(member), pk=pk)


def _editable_note_or_404(member, pk):
    note = get_object_or_404(
        InvestmentNote.objects.select_related("member", "note_type"),
        pk=pk,
        family=member.family,
    )
    if note.member_id != member.id:
        raise Http404
    return note


def _can_write(member):
    return member.role != FamilyMember.ROLE_VIEWER


@login_required
def index(request):
    member = _current_member(request)
    if member is None:
        return _membership_required_response(request)

    category = request.GET.get("category", "").strip()
    query = request.GET.get("q", "").strip()

    accessible = _accessible_notes(member)
    category_types = list(
        InvestmentNoteType.objects.filter(
            Q(is_active=True) | Q(investment_notes__in=accessible)
        )
        .distinct()
        .order_by("sort_order", "id")
    )
    valid_categories = {note_type.code for note_type in category_types}
    if category not in valid_categories:
        category = ""

    today = timezone.localdate()
    category_counts = {
        note_type.code: accessible.filter(note_type=note_type).count()
        for note_type in category_types
    }
    stats = {
        "total": accessible.count(),
        "this_month": accessible.filter(
            note_date__year=today.year,
            note_date__month=today.month,
        ).count(),
        "trade": category_counts.get(InvestmentNote.TYPE_TRADE, 0),
        "research": category_counts.get(InvestmentNote.TYPE_RESEARCH, 0),
    }

    notes = accessible
    if category:
        notes = notes.filter(note_type__code=category)
    notes = list(notes)
    if query:
        normalized_query = query.casefold()
        notes = [
            note
            for note in notes
            if normalized_query in note.title.casefold()
            or normalized_query in note.content.casefold()
            or any(normalized_query in str(tag).casefold() for tag in (note.tags or []))
        ]

    page_obj = Paginator(notes, 12).get_page(request.GET.get("page"))
    return render(
        request,
        "notes/index.html",
        {
            "page_obj": page_obj,
            "query": query,
            "selected_category": category,
            "category_choices": category_types,
            "category_counts": category_counts,
            "stats": stats,
            "can_write": _can_write(member),
        },
    )


@login_required
def detail(request, pk):
    member = _current_member(request)
    if member is None:
        return _membership_required_response(request)
    note = _accessible_note_or_404(member, pk)
    return render(
        request,
        "notes/detail.html",
        {
            "note": note,
            "can_edit": _can_write(member) and note.member_id == member.id,
        },
    )


@login_required
def create(request):
    member = _current_member(request)
    if member is None:
        return _membership_required_response(request)
    if not _can_write(member):
        return HttpResponseForbidden("当前家庭成员是只读角色，不能新建笔记。")

    if request.method == "POST":
        form = InvestmentNoteForm(request.POST)
        if form.is_valid():
            note = form.save(commit=False)
            note.family = member.family
            note.member = member
            note.save()
            messages.success(request, "投资笔记已保存。")
            return redirect("notes:detail", pk=note.pk)
    else:
        form = InvestmentNoteForm()
    return render(
        request,
        "notes/form.html",
        {"form": form, "title": "新建投资笔记", "submit_label": "保存笔记"},
    )


@login_required
def edit(request, pk):
    member = _current_member(request)
    if member is None:
        return _membership_required_response(request)
    if not _can_write(member):
        return HttpResponseForbidden("当前家庭成员是只读角色，不能编辑笔记。")
    note = _editable_note_or_404(member, pk)

    if request.method == "POST":
        form = InvestmentNoteForm(request.POST, instance=note)
        if form.is_valid():
            form.save()
            messages.success(request, "投资笔记已更新。")
            return redirect("notes:detail", pk=note.pk)
    else:
        form = InvestmentNoteForm(instance=note)
    return render(
        request,
        "notes/form.html",
        {
            "form": form,
            "note": note,
            "title": "编辑投资笔记",
            "submit_label": "保存修改",
        },
    )


@login_required
@require_POST
def delete(request, pk):
    member = _current_member(request)
    if member is None:
        return _membership_required_response(request)
    if not _can_write(member):
        return HttpResponseForbidden("当前家庭成员是只读角色，不能删除笔记。")
    note = _editable_note_or_404(member, pk)
    note.delete()
    messages.success(request, "投资笔记已删除。")
    return redirect("notes:index")
