from uuid import uuid4

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Q
from django.http import Http404, HttpResponseForbidden
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from family_core.models import FamilyMember
from knowledge.permissions import current_member

from .forms import (
    ConversationCreateForm,
    FamilyFinancialAuthorizationForm,
    GlobalAiPromptForm,
    MemoryCreateForm,
    MemoryRevisionForm,
    OutboundAuthorizationForm,
)
from .global_ai_jobs import (
    GlobalAiJobError,
    launch_global_ai_request,
    provider_configuration,
)
from .global_ai_services import (
    GlobalAiServiceError,
    confirm_memory,
    create_answer_share_preview,
    create_conversation,
    delete_memory,
    publish_answer_share,
    propose_memory,
    refresh_answer_share_state,
    revise_memory,
    set_family_financial_cloud_authorization,
    set_conversation_archived,
    shared_answer_payload,
    submit_global_ai_request,
    append_conversation_message,
    cancel_global_ai_request,
    withdraw_answer_share,
)
from .models import (
    AiAnalysisRequest,
    AiAnswerShare,
    AiConversation,
    AiConversationMessage,
    AiFamilyOutboundAuthorization,
    AiMemory,
    AiOutboundAuthorization,
    AiProvider,
)


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
    provider_ready = False
    provider_error = ""
    if global_provider:
        try:
            provider_configuration(global_provider)
            provider_ready = True
        except GlobalAiJobError as exc:
            provider_error = str(exc)
    grants = set()
    if global_provider:
        grants = set(AiOutboundAuthorization.objects.filter(
            family=member.family,
            member=member,
            provider=global_provider,
            is_allowed=True,
        ).values_list("data_type", flat=True))
    conversation_allowed = AiOutboundAuthorization.DATA_CONVERSATION in grants
    family_financial_allowed = False
    if global_provider:
        family_financial_allowed = AiFamilyOutboundAuthorization.objects.filter(
            family=member.family,
            provider=global_provider,
            data_type=AiFamilyOutboundAuthorization.DATA_FINANCIAL,
            is_allowed=True,
        ).exists()
    active_family_scope = bool(
        active_conversation
        and active_conversation.financial_scope == AiConversation.SCOPE_FAMILY
    )
    conversation_messages = []
    conversation_requests = []
    request_in_flight = False
    if active_conversation:
        conversation_messages = list(active_conversation.messages.all())
        shares = {
            share.source_message_id: share
            for share in AiAnswerShare.objects.filter(
                owner=member,
                source_message__conversation=active_conversation,
                status__in=[
                    AiAnswerShare.STATUS_DRAFT,
                    AiAnswerShare.STATUS_ACTIVE,
                    AiAnswerShare.STATUS_PAUSED,
                ],
            )
        }
        for item in conversation_messages:
            item.current_share = shares.get(item.pk)
        request_queryset = active_conversation.requests.filter(
            member=member, module="global_ai"
        )
        request_in_flight = request_queryset.filter(status__in=[
            AiAnalysisRequest.STATUS_PENDING,
            AiAnalysisRequest.STATUS_RUNNING,
            AiAnalysisRequest.STATUS_CANCEL_REQUESTED,
        ]).exists()
        conversation_requests = request_queryset.select_related(
            "provider", "result"
        ).order_by("-created_at")[:10]

    legacy_requests = AiAnalysisRequest.objects.filter(
        family=member.family
    ).exclude(module="global_ai").select_related("member", "provider").order_by("-created_at")[:20]

    return {
        "current_member": member,
        "conversations": conversations,
        "active_conversation": active_conversation,
        "conversation_messages": conversation_messages,
        "conversation_requests": conversation_requests,
        "request_in_flight": request_in_flight,
        "confirmed_memories": confirmed_memories,
        "candidate_memories": candidate_memories,
        "conversation_form": ConversationCreateForm(
            allow_family=member.role != FamilyMember.ROLE_VIEWER
        ),
        "memory_form": MemoryCreateForm(
            allow_family=member.role != FamilyMember.ROLE_VIEWER
        ),
        "prompt_form": GlobalAiPromptForm(initial={"idempotency_key": uuid4().hex}),
        "authorization_form": OutboundAuthorizationForm(initial={"allowed_data_types": sorted(grants)}),
        "authorization_labels": AiOutboundAuthorization.DATA_TYPE_CHOICES,
        "allowed_data_types": grants,
        "global_provider": global_provider,
        "provider_ready": provider_ready,
        "provider_error": provider_error,
        "family_financial_allowed": family_financial_allowed,
        "can_manage_family_financial_authorization": member.role == FamilyMember.ROLE_ADMIN,
        "family_financial_authorization_form": FamilyFinancialAuthorizationForm(
            initial={"is_allowed": family_financial_allowed}
        ),
        "active_family_scope": active_family_scope,
        "model_ready": (
            provider_ready
            and conversation_allowed
            and (not active_family_scope or family_financial_allowed)
        ),
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
    messages.success(request, "私人对话已建立。")
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
def outbound_authorization_update(request):
    member = current_member(request)
    if member is None:
        return _membership_required_response(request)
    provider = _global_provider()
    if provider is None:
        messages.error(request, "全局 AI 服务商尚未配置。")
        return redirect("ai_analysis:index")
    form = OutboundAuthorizationForm(request.POST)
    if not form.is_valid():
        messages.error(request, "外发授权设置不可用。")
        return redirect("ai_analysis:index")
    allowed = set(form.cleaned_data["allowed_data_types"])
    with transaction.atomic():
        for data_type, _label in AiOutboundAuthorization.DATA_TYPE_CHOICES:
            AiOutboundAuthorization.objects.update_or_create(
                family=member.family,
                member=member,
                provider=provider,
                data_type=data_type,
                defaults={"is_allowed": data_type in allowed},
            )
    messages.success(request, "云端 AI 资料授权已更新。")
    return redirect("ai_analysis:index")


@login_required
@require_POST
def family_financial_authorization_update(request):
    member = current_member(request)
    if member is None:
        return _membership_required_response(request)
    if member.role != FamilyMember.ROLE_ADMIN:
        return HttpResponseForbidden("只有家庭管理员可以修改全家财务云端授权。")
    provider = _global_provider()
    if provider is None:
        messages.error(request, "全局 AI 服务商尚未配置。")
        return redirect("ai_analysis:index")
    form = FamilyFinancialAuthorizationForm(request.POST)
    if not form.is_valid():
        messages.error(request, "全家财务云端授权设置不可用。")
        return redirect("ai_analysis:index")
    try:
        authorization = set_family_financial_cloud_authorization(
            member,
            provider=provider,
            is_allowed=form.cleaned_data["is_allowed"],
        )
    except GlobalAiServiceError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(
            request,
            "已允许家庭成员使用全家财务咨询云端 AI。"
            if authorization.is_allowed
            else "已停止向云端 AI 发送全家财务资料。",
        )
    return redirect("ai_analysis:index")


@login_required
@require_POST
def conversation_ask(request, conversation_id):
    member = current_member(request)
    if member is None:
        return _membership_required_response(request)
    conversation = AiConversation.objects.filter(
        pk=conversation_id, family=member.family, member=member
    ).first()
    if conversation is None:
        raise Http404
    form = GlobalAiPromptForm(request.POST)
    if not form.is_valid():
        messages.error(request, "问题不能为空，且不能超过 4000 字。")
        return redirect(_conversation_url(conversation))
    try:
        provider, config = provider_configuration()
        with transaction.atomic():
            analysis_request, created = submit_global_ai_request(
                member,
                conversation_id=conversation.pk,
                idempotency_key=form.cleaned_data["idempotency_key"],
                prompt=form.cleaned_data["content"],
                provider=provider,
                scope={"config_fingerprint": config["fingerprint"]},
            )
            if created:
                append_conversation_message(
                    member,
                    conversation_id=conversation.pk,
                    role=AiConversationMessage.ROLE_USER,
                    content=form.cleaned_data["content"],
                )
                transaction.on_commit(lambda: launch_global_ai_request(analysis_request.pk))
    except (GlobalAiJobError, GlobalAiServiceError) as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "问题已提交，回答完成后会显示在这段对话中。" if created else "这条问题已经提交过了。")
    return redirect(_conversation_url(conversation))


@login_required
@require_POST
def request_cancel(request, request_id):
    member = current_member(request)
    if member is None:
        return _membership_required_response(request)
    conversation_id = AiAnalysisRequest.objects.filter(
        pk=request_id, member=member, family=member.family, module="global_ai"
    ).values_list("conversation_id", flat=True).first()
    try:
        cancel_global_ai_request(member, request_id=request_id)
    except GlobalAiServiceError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "已提交停止请求。")
    conversation = AiConversation.objects.filter(pk=conversation_id, member=member).first()
    return redirect(_conversation_url(conversation))


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


@login_required
@require_POST
def answer_share_preview_create(request, message_id):
    member = current_member(request)
    if member is None:
        return _membership_required_response(request)
    try:
        share, created = create_answer_share_preview(member, message_id=message_id)
    except GlobalAiServiceError as exc:
        messages.error(request, str(exc))
        return redirect("ai_analysis:index")
    if created:
        messages.success(request, "已生成站内预览，请核对后再分享给家人。")
    return redirect("ai_analysis:answer_share_preview", share_id=share.pk)


@login_required
def answer_share_preview(request, share_id):
    member = current_member(request)
    if member is None:
        return _membership_required_response(request)
    share = AiAnswerShare.objects.select_related("owner", "source_message").filter(
        pk=share_id, family=member.family, owner=member
    ).first()
    if share is None:
        raise Http404
    return render(request, "ai_analysis/share_preview.html", {"share": share})


@login_required
@require_POST
def answer_share_publish(request, share_id):
    member = current_member(request)
    if member is None:
        return _membership_required_response(request)
    try:
        share = publish_answer_share(member, share_id=share_id)
    except GlobalAiServiceError as exc:
        messages.error(request, str(exc))
        return redirect("ai_analysis:answer_share_preview", share_id=share_id)
    messages.success(request, "这条回答已在家庭工作台内分享。")
    return redirect("ai_analysis:answer_share_detail", share_id=share.pk)


@login_required
def answer_share_detail(request, share_id):
    member = current_member(request)
    if member is None:
        return _membership_required_response(request)
    share = AiAnswerShare.objects.filter(pk=share_id, family=member.family).first()
    if share is None:
        raise Http404
    if share.status != AiAnswerShare.STATUS_ACTIVE and share.owner_id != member.pk:
        raise Http404
    try:
        payload = shared_answer_payload(member, share_id=share_id)
    except GlobalAiServiceError:
        return render(
            request,
            "ai_analysis/share_unavailable.html",
            {"share": share, "is_owner": share.owner_id == member.pk},
            status=410,
        )
    return render(
        request,
        "ai_analysis/share_detail.html",
        {"share": share, "shared_answer": payload, "is_owner": share.owner_id == member.pk},
    )


@login_required
@require_POST
def answer_share_refresh(request, share_id):
    member = current_member(request)
    if member is None:
        return _membership_required_response(request)
    try:
        share = refresh_answer_share_state(member, share_id=share_id)
    except GlobalAiServiceError as exc:
        messages.error(request, str(exc))
    else:
        if share.status == AiAnswerShare.STATUS_PAUSED:
            messages.warning(request, "依据已变化，这条分享已暂停。请重新生成预览。")
        else:
            messages.success(request, "依据仍可由家庭成员查看。")
    return redirect("ai_analysis:answer_share_preview", share_id=share_id)


@login_required
@require_POST
def answer_share_withdraw(request, share_id):
    member = current_member(request)
    if member is None:
        return _membership_required_response(request)
    try:
        share = withdraw_answer_share(member, share_id=share_id)
    except GlobalAiServiceError as exc:
        messages.error(request, str(exc))
        return redirect("ai_analysis:index")
    messages.success(request, "这条回答已撤回。")
    return redirect(_conversation_url(share.source_message.conversation))
