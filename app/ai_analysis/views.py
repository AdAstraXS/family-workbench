from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from family_core.models import FamilyMember
from knowledge.permissions import current_member

from .forms import ConversationCreateForm, MemoryCreateForm, MemoryRevisionForm
from .global_ai_services import (
    GlobalAiServiceError,
    confirm_memory,
    create_conversation,
    delete_memory,
    propose_memory,
    revise_memory,
    set_conversation_archived,
)
from .models import AiAnalysisRequest, AiConversation, AiMemory, AiProvider


def _membership_required_response(request):
    return render(request, "ai_analysis/membership_required.html", status=403)


def _conversation_url(conversation=None):
    if conversation is None:
        return reverse("ai_analysis:index")
    return reverse("ai_analysis:conversation", args=[conversation.pk])


def _global_provider():
    for provider in AiProvider.objects.filter(is_active=True).order_by("name", "pk"):
        if provider.extra_data.get("global_ai_enabled"):
            return provider
    return None


def _workbench_context(member, *, active_conversation=None):
    conversations = list(
        AiConversation.objects.filter(member=member, is_archived=False).order_by(
            "-updated_at", "-pk"
        )[:50]
    )
    if active_conversation and all(item.pk != active_conversation.pk for item in conversations):
        conversations.insert(0, active_conversation)

    accessible_memories = AiMemory.objects.filter(family=member.family).filter(
        Q(visibility=AiMemory.VISIBILITY_FAMILY)
        | Q(visibility=AiMemory.VISIBILITY_PERSONAL, owner=member)
    )
    confirmed_memories = accessible_memories.filter(
        status=AiMemory.STATUS_CONFIRMED
    ).select_related("created_by", "confirmed_by").order_by("visibility", "-updated_at")
    candidate_memories = accessible_memories.filter(
        status=AiMemory.STATUS_CANDIDATE
    ).select_related("created_by").order_by("visibility", "-created_at")

    global_provider = _global_provider()
    conversation_messages = []
    conversation_requests = []
    if active_conversation:
        conversation_messages = active_conversation.messages.all()
        conversation_requests = active_conversation.requests.filter(
            member=member, module="global_ai"
        ).select_related("provider", "result").order_by("-created_at")[:10]

    legacy_requests = AiAnalysisRequest.objects.filter(
        family=member.family
    ).exclude(module="global_ai").select_related("member", "provider").order_by("-created_at")[:20]

    return {
        "current_member": member,
        "conversations": conversations,
        "active_conversation": active_conversation,
        "conversation_messages": conversation_messages,
        "conversation_requests": conversation_requests,
        "confirmed_memories": confirmed_memories,
        "candidate_memories": candidate_memories,
        "conversation_form": ConversationCreateForm(),
        "memory_form": MemoryCreateForm(
            allow_family=member.role != FamilyMember.ROLE_VIEWER
        ),
        "global_provider": global_provider,
        "model_ready": False,
        "legacy_requests": legacy_requests,
        "recent_requests": legacy_requests,
    }


@login_required
def index(request):
    member = current_member(request)
    if member is None:
        return _membership_required_response(request)
    return render(request, "ai_analysis/index.html", _workbench_context(member))


@login_required
def conversation_detail(request, conversation_id):
    member = current_member(request)
    if member is None:
        return _membership_required_response(request)
    conversation = AiConversation.objects.filter(
        pk=conversation_id, family=member.family, member=member
    ).first()
    if conversation is None:
        raise Http404
    return render(
        request,
        "ai_analysis/index.html",
        _workbench_context(member, active_conversation=conversation),
    )


@login_required
@require_POST
def conversation_create(request):
    member = current_member(request)
    if member is None:
        return _membership_required_response(request)
    form = ConversationCreateForm(request.POST)
    if not form.is_valid():
        messages.error(request, "请检查对话名称和财务范围。")
        return redirect("ai_analysis:index")
    try:
        conversation = create_conversation(member, **form.cleaned_data)
    except GlobalAiServiceError as exc:
        messages.error(request, str(exc))
        return redirect("ai_analysis:index")
    messages.success(request, "私人对话已建立。模型接入后，可以从这里开始提问。")
    return redirect(_conversation_url(conversation))


@login_required
@require_POST
def conversation_archive(request, conversation_id):
    member = current_member(request)
    if member is None:
        return _membership_required_response(request)
    try:
        set_conversation_archived(member, conversation_id=conversation_id)
    except GlobalAiServiceError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "对话已归档。")
    return redirect("ai_analysis:index")


@login_required
@require_POST
def memory_create(request):
    member = current_member(request)
    if member is None:
        return _membership_required_response(request)
    form = MemoryCreateForm(request.POST)
    if not form.is_valid():
        messages.error(request, "请填写需要记住的内容并选择使用范围。")
        return redirect("ai_analysis:index")
    try:
        propose_memory(member, source_note="成员在 AI 助手页面添加", **form.cleaned_data)
    except GlobalAiServiceError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "已加入待确认。确认后才会用于后续对话。")
    return redirect("ai_analysis:index")


def _memory_action(request, memory_id, action):
    member = current_member(request)
    if member is None:
        return _membership_required_response(request), False
    try:
        action(member, memory_id=memory_id)
    except GlobalAiServiceError as exc:
        messages.error(request, str(exc))
        return redirect("ai_analysis:index"), False
    return redirect("ai_analysis:index"), True


@login_required
@require_POST
def memory_confirm(request, memory_id):
    response, succeeded = _memory_action(request, memory_id, confirm_memory)
    if succeeded:
        messages.success(request, "记录已确认，后续对话可以使用。")
    return response


@login_required
@require_POST
def memory_revise(request, memory_id):
    member = current_member(request)
    if member is None:
        return _membership_required_response(request)
    form = MemoryRevisionForm(request.POST)
    if not form.is_valid():
        messages.error(request, "修改后的内容不能为空。")
        return redirect("ai_analysis:index")
    try:
        revise_memory(member, memory_id=memory_id, content=form.cleaned_data["content"])
    except GlobalAiServiceError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "记录已更新，旧版本仍保留在历史中。")
    return redirect("ai_analysis:index")


@login_required
@require_POST
def memory_delete(request, memory_id):
    response, succeeded = _memory_action(request, memory_id, delete_memory)
    if succeeded:
        messages.success(request, "记录已删除，后续对话不再使用。")
    return response
