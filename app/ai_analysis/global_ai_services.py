"""Permission-safe conversation, memory, outbound, and request lifecycle services."""

from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from uuid import uuid4

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from family_core.models import FamilyMember
from knowledge.models import KnowledgeDocument

from .models import (
    AiAnalysisRequest,
    AiAnalysisResult,
    AiConversation,
    AiConversationMessage,
    AiMemory,
    AiOutboundAuthorization,
    AiProvider,
)
from .read_tools import GlobalAiReadError, knowledge_revision


GLOBAL_AI_MODULE = "global_ai"
GLOBAL_AI_ANALYSIS_TYPE = "chat_v1"
VALID_DATA_TYPES = {choice[0] for choice in AiOutboundAuthorization.DATA_TYPE_CHOICES}
VALID_MESSAGE_ROLES = {choice[0] for choice in AiConversationMessage.ROLE_CHOICES}


class GlobalAiServiceError(ValueError):
    """Raised when a global AI action is invalid or unavailable to the actor."""


def _validate_actor(actor):
    if not actor or not actor.pk or not actor.is_active or not actor.family_id:
        raise GlobalAiServiceError("当前成员不可用。")


def _conversation_for(actor, conversation_id, *, for_update=False):
    _validate_actor(actor)
    queryset = AiConversation.objects.filter(
        pk=conversation_id,
        family=actor.family,
        member=actor,
    )
    if for_update:
        queryset = queryset.select_for_update()
    conversation = queryset.first()
    if conversation is None:
        raise GlobalAiServiceError("会话不可用。")
    return conversation


def create_conversation(actor, *, financial_scope=AiConversation.SCOPE_PERSONAL, title=""):
    _validate_actor(actor)
    if financial_scope not in dict(AiConversation.SCOPE_CHOICES):
        raise GlobalAiServiceError("不支持的财务范围。")
    return AiConversation.objects.create(
        family=actor.family,
        member=actor,
        financial_scope=financial_scope,
        title=(title or "").strip()[:200],
    )


def set_conversation_archived(actor, *, conversation_id, archived=True):
    with transaction.atomic():
        conversation = _conversation_for(actor, conversation_id, for_update=True)
        conversation.is_archived = bool(archived)
        conversation.save(update_fields=["is_archived", "updated_at"])
    return conversation


def append_conversation_message(
    actor,
    *,
    conversation_id,
    role,
    content,
    data_types=None,
    evidence_refs=None,
):
    if role not in VALID_MESSAGE_ROLES or not isinstance(content, str) or not content.strip():
        raise GlobalAiServiceError("消息内容不可用。")
    data_types = list(data_types or [])
    if any(item not in VALID_DATA_TYPES for item in data_types):
        raise GlobalAiServiceError("消息包含不支持的数据类型。")
    evidence_refs = list(evidence_refs or [])
    with transaction.atomic():
        conversation = _conversation_for(actor, conversation_id, for_update=True)
        if conversation.is_archived:
            raise GlobalAiServiceError("已归档会话不能新增消息。")
        last = conversation.messages.order_by("-sequence").values_list("sequence", flat=True).first()
        message = AiConversationMessage.objects.create(
            conversation=conversation,
            sequence=(last or 0) + 1,
            role=role,
            content=content.strip(),
            data_types=sorted(set(data_types)),
            evidence_refs=evidence_refs,
        )
        conversation.save(update_fields=["updated_at"])
    return message


def _memory_for(actor, memory_id, *, for_update=False):
    _validate_actor(actor)
    queryset = AiMemory.objects.filter(pk=memory_id, family=actor.family).filter(
        Q(visibility=AiMemory.VISIBILITY_FAMILY)
        | Q(visibility=AiMemory.VISIBILITY_PERSONAL, owner=actor)
    )
    if for_update:
        queryset = queryset.select_for_update()
    memory = queryset.first()
    if memory is None:
        raise GlobalAiServiceError("记忆不可用。")
    return memory


def propose_memory(
    actor,
    *,
    content,
    visibility=AiMemory.VISIBILITY_PERSONAL,
    source_note="",
):
    _validate_actor(actor)
    if visibility not in dict(AiMemory.VISIBILITY_CHOICES):
        raise GlobalAiServiceError("不支持的记忆范围。")
    if (
        visibility == AiMemory.VISIBILITY_FAMILY
        and actor.role == FamilyMember.ROLE_VIEWER
    ):
        raise GlobalAiServiceError("查看者只能维护自己的个人背景。")
    if not isinstance(content, str) or not content.strip():
        raise GlobalAiServiceError("记忆内容不能为空。")
    return AiMemory.objects.create(
        family=actor.family,
        owner=actor if visibility == AiMemory.VISIBILITY_PERSONAL else None,
        created_by=actor,
        visibility=visibility,
        content=content.strip(),
        source_note=(source_note or "").strip()[:300],
    )


def confirm_memory(actor, *, memory_id):
    with transaction.atomic():
        memory = _memory_for(actor, memory_id, for_update=True)
        if (
            memory.visibility == AiMemory.VISIBILITY_FAMILY
            and actor.role == FamilyMember.ROLE_VIEWER
        ):
            raise GlobalAiServiceError("查看者不能修改家庭共同记录。")
        if memory.status != AiMemory.STATUS_CANDIDATE:
            raise GlobalAiServiceError("只有待确认记忆可以确认。")
        memory.status = AiMemory.STATUS_CONFIRMED
        memory.confirmed_by = actor
        memory.confirmed_at = timezone.now()
        memory.save(update_fields=["status", "confirmed_by", "confirmed_at", "updated_at"])
    return memory


def revise_memory(actor, *, memory_id, content):
    if not isinstance(content, str) or not content.strip():
        raise GlobalAiServiceError("记忆内容不能为空。")
    with transaction.atomic():
        memory = _memory_for(actor, memory_id, for_update=True)
        if (
            memory.visibility == AiMemory.VISIBILITY_FAMILY
            and actor.role == FamilyMember.ROLE_VIEWER
        ):
            raise GlobalAiServiceError("查看者不能修改家庭共同记录。")
        if memory.status != AiMemory.STATUS_CONFIRMED:
            raise GlobalAiServiceError("只有已确认记忆可以修改。")
        replacement = AiMemory.objects.create(
            family=memory.family,
            owner=memory.owner,
            created_by=actor,
            confirmed_by=actor,
            visibility=memory.visibility,
            status=AiMemory.STATUS_CONFIRMED,
            content=content.strip(),
            source_note=f"修改自记忆 #{memory.pk}",
            version=memory.version + 1,
            supersedes=memory,
            confirmed_at=timezone.now(),
        )
        memory.status = AiMemory.STATUS_SUPERSEDED
        memory.save(update_fields=["status", "updated_at"])
    return replacement


def delete_memory(actor, *, memory_id):
    with transaction.atomic():
        memory = _memory_for(actor, memory_id, for_update=True)
        if (
            memory.visibility == AiMemory.VISIBILITY_FAMILY
            and actor.role == FamilyMember.ROLE_VIEWER
        ):
            raise GlobalAiServiceError("查看者不能修改家庭共同记录。")
        if memory.status not in {AiMemory.STATUS_CANDIDATE, AiMemory.STATUS_CONFIRMED}:
            raise GlobalAiServiceError("记忆已经不可删除。")
        memory.status = AiMemory.STATUS_DELETED
        memory.deleted_at = timezone.now()
        memory.save(update_fields=["status", "deleted_at", "updated_at"])
    return memory


def confirmed_memory_context(actor):
    _validate_actor(actor)
    memories = AiMemory.objects.filter(
        family=actor.family,
        status=AiMemory.STATUS_CONFIRMED,
    ).filter(
        Q(visibility=AiMemory.VISIBILITY_FAMILY)
        | Q(visibility=AiMemory.VISIBILITY_PERSONAL, owner=actor)
    )
    return [
        {
            "memory_id": memory.pk,
            "visibility": memory.visibility,
            "version": memory.version,
            "content": memory.content,
            "status": memory.status,
            "source_note": memory.source_note,
            "created_by_id": memory.created_by_id,
            "confirmed_by_id": memory.confirmed_by_id,
            "confirmed_at": memory.confirmed_at.isoformat() if memory.confirmed_at else None,
            "updated_at": memory.updated_at.isoformat(),
        }
        for memory in memories.order_by("visibility", "pk")
    ]


def _require_cloud_grants(actor, provider, data_types):
    required = set(data_types) | {AiOutboundAuthorization.DATA_CONVERSATION}
    allowed = set(
        AiOutboundAuthorization.objects.filter(
            family=actor.family,
            member=actor,
            provider=provider,
            is_allowed=True,
            data_type__in=required,
        ).values_list("data_type", flat=True)
    )
    missing = sorted(required - allowed)
    if missing:
        raise GlobalAiServiceError("当前服务商未获准接收本次上下文。")


def prepare_conversation_context(actor, *, conversation_id, provider):
    conversation = _conversation_for(actor, conversation_id)
    if not provider or not provider.pk or not provider.is_active:
        raise GlobalAiServiceError("AI 服务商不可用。")
    messages = list(conversation.messages.order_by("sequence", "pk"))
    all_types = {
        data_type
        for message in messages
        for data_type in (message.data_types or [])
    }
    if any(data_type not in VALID_DATA_TYPES for data_type in all_types):
        raise GlobalAiServiceError("历史消息包含不支持的数据类型。")
    validated_refs = []
    for message in messages:
        refs = message.evidence_refs or []
        ref_types = set()
        for reference in refs:
            if not isinstance(reference, dict):
                raise GlobalAiServiceError("证据引用不可用。")
            kind = reference.get("kind")
            if kind == "knowledge":
                ref_types.add(AiOutboundAuthorization.DATA_KNOWLEDGE)
                try:
                    knowledge_revision(
                        actor,
                        document_id=reference.get("document_id"),
                        revision_id=reference.get("revision_id"),
                        include_pending=reference.get("include_pending") is True,
                    )
                except GlobalAiReadError as exc:
                    raise GlobalAiServiceError("历史知识证据已经不可用。") from exc
            elif kind == "memory":
                ref_types.add(AiOutboundAuthorization.DATA_MEMORY)
                memory = _memory_for(actor, reference.get("memory_id"))
                if memory.status != AiMemory.STATUS_CONFIRMED:
                    raise GlobalAiServiceError("历史记忆已经不可用。")
            else:
                raise GlobalAiServiceError("证据引用不可用。")
        declared_ref_types = set(message.data_types or {}) & {
            AiOutboundAuthorization.DATA_KNOWLEDGE,
            AiOutboundAuthorization.DATA_MEMORY,
        }
        if declared_ref_types != ref_types:
            raise GlobalAiServiceError("知识或记忆上下文缺少可复核的版本引用。")
        all_types.update(ref_types)
        validated_refs.append((message, refs))

    if provider.execution_location == AiProvider.LOCATION_CLOUD:
        _require_cloud_grants(actor, provider, all_types)

    payload = []
    for message, refs in validated_refs:
        if provider.execution_location == AiProvider.LOCATION_CLOUD:
            for reference in refs:
                if reference.get("kind") != "knowledge":
                    continue
                cloud_allowed = KnowledgeDocument.objects.filter(
                    pk=reference.get("document_id"),
                    source__allow_cloud_ai=True,
                ).exists()
                if not cloud_allowed:
                    raise GlobalAiServiceError("知识来源未允许发送正文给云端 AI。")
        payload.append(
            {
                "message_id": message.pk,
                "role": message.role,
                "content": message.content,
                "data_types": message.data_types,
                "evidence_refs": refs,
            }
        )
    return {
        "conversation_id": conversation.pk,
        "provider_id": provider.pk,
        "execution_location": provider.execution_location,
        "messages": payload,
    }


def _request_fingerprint(*, conversation, provider, prompt, scope, sanitized_input):
    payload = {
        "conversation_id": conversation.pk,
        "provider_id": provider.pk if provider else None,
        "prompt": prompt,
        "scope": scope,
        "sanitized_input": sanitized_input,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()


def submit_global_ai_request(
    actor,
    *,
    conversation_id,
    idempotency_key,
    prompt,
    provider=None,
    scope=None,
    sanitized_input=None,
):
    conversation = _conversation_for(actor, conversation_id)
    if provider is not None and (not provider.pk or not provider.is_active):
        raise GlobalAiServiceError("AI 服务商不可用。")
    if conversation.is_archived:
        raise GlobalAiServiceError("已归档会话不能提交新请求。")
    if provider is not None:
        prepare_conversation_context(actor, conversation_id=conversation.pk, provider=provider)
    if not isinstance(idempotency_key, str) or not idempotency_key.strip() or len(idempotency_key) > 100:
        raise GlobalAiServiceError("幂等键不可用。")
    if not isinstance(prompt, str) or not prompt.strip():
        raise GlobalAiServiceError("请求内容不能为空。")
    key = idempotency_key.strip()
    scope = dict(scope or {})
    if {"actor", "member_id", "family_id", "financial_scope"} & set(scope):
        raise GlobalAiServiceError("请求范围不能覆盖登录成员或会话范围。")
    scope = {"financial_scope": conversation.financial_scope, **scope}
    sanitized_input = dict(sanitized_input or {})
    fingerprint = _request_fingerprint(
        conversation=conversation,
        provider=provider,
        prompt=prompt.strip(),
        scope=scope,
        sanitized_input=sanitized_input,
    )
    try:
        with transaction.atomic():
            FamilyMember.objects.select_for_update().get(
                pk=actor.pk,
                family=actor.family,
                is_active=True,
            )
            existing = AiAnalysisRequest.objects.filter(
                family=actor.family,
                member=actor,
                module=GLOBAL_AI_MODULE,
                idempotency_key=key,
            ).first()
            if existing:
                if existing.request_fingerprint != fingerprint:
                    raise GlobalAiServiceError("该幂等键已用于不同请求。")
                return existing, False
            if provider:
                raw_limit = (provider.extra_data or {}).get("global_ai_daily_request_limit")
                if raw_limit is not None:
                    if not isinstance(raw_limit, int) or isinstance(raw_limit, bool) or raw_limit < 1:
                        raise GlobalAiServiceError("服务商用量上限配置不可用。")
                    used = AiAnalysisRequest.objects.filter(
                        family=actor.family,
                        member=actor,
                        provider=provider,
                        module=GLOBAL_AI_MODULE,
                        created_at__date=timezone.localdate(),
                    ).count()
                    if used >= raw_limit:
                        raise GlobalAiServiceError("当前服务商今天的全局 AI 请求已达到上限。")
            request = AiAnalysisRequest.objects.create(
                family=actor.family,
                member=actor,
                conversation=conversation,
                provider=provider,
                module=GLOBAL_AI_MODULE,
                analysis_type=GLOBAL_AI_ANALYSIS_TYPE,
                scope=scope,
                prompt=prompt.strip(),
                sanitized_input=sanitized_input,
                idempotency_key=key,
                request_fingerprint=fingerprint,
            )
    except IntegrityError:
        request = AiAnalysisRequest.objects.get(
            family=actor.family,
            member=actor,
            module=GLOBAL_AI_MODULE,
            idempotency_key=key,
        )
        if request.request_fingerprint != fingerprint:
            raise GlobalAiServiceError("该幂等键已用于不同请求。")
        return request, False
    return request, True


def claim_global_ai_request(*, request_id):
    with transaction.atomic():
        request = AiAnalysisRequest.objects.select_for_update().filter(
            pk=request_id,
            module=GLOBAL_AI_MODULE,
            analysis_type=GLOBAL_AI_ANALYSIS_TYPE,
        ).first()
        if request is None or request.status != AiAnalysisRequest.STATUS_PENDING:
            raise GlobalAiServiceError("请求不可执行。")
        request.status = AiAnalysisRequest.STATUS_RUNNING
        request.execution_token = uuid4().hex
        request.started_at = timezone.now()
        request.save(update_fields=["status", "execution_token", "started_at", "updated_at"])
    return request.execution_token


def cancel_global_ai_request(actor, *, request_id):
    _validate_actor(actor)
    with transaction.atomic():
        request = AiAnalysisRequest.objects.select_for_update().filter(
            pk=request_id,
            family=actor.family,
            member=actor,
            module=GLOBAL_AI_MODULE,
        ).first()
        if request is None:
            raise GlobalAiServiceError("请求不可用。")
        if request.status == AiAnalysisRequest.STATUS_PENDING:
            request.status = AiAnalysisRequest.STATUS_CANCELLED
            request.finished_at = timezone.now()
        elif request.status == AiAnalysisRequest.STATUS_RUNNING:
            request.status = AiAnalysisRequest.STATUS_CANCEL_REQUESTED
        else:
            raise GlobalAiServiceError("请求当前不能停止。")
        request.save(update_fields=["status", "finished_at", "updated_at"])
    return request


def global_ai_request_state(actor, *, request_id):
    _validate_actor(actor)
    request = AiAnalysisRequest.objects.filter(
        pk=request_id,
        family=actor.family,
        member=actor,
        module=GLOBAL_AI_MODULE,
        analysis_type=GLOBAL_AI_ANALYSIS_TYPE,
    ).first()
    if request is None:
        raise GlobalAiServiceError("请求不可用。")
    result = AiAnalysisResult.objects.filter(request=request).first()
    return {
        "request_id": request.pk,
        "conversation_id": request.conversation_id,
        "status": request.status,
        "error_message": request.error_message,
        "started_at": request.started_at.isoformat() if request.started_at else None,
        "finished_at": request.finished_at.isoformat() if request.finished_at else None,
        "result_text": result.result_text if result else None,
        "tokens_used": result.tokens_used if result else None,
        "cost_estimate": str(result.cost_estimate) if result and result.cost_estimate is not None else None,
    }


def acknowledge_global_ai_cancellation(*, request_id, execution_token):
    with transaction.atomic():
        request = AiAnalysisRequest.objects.select_for_update().filter(
            pk=request_id,
            module=GLOBAL_AI_MODULE,
            analysis_type=GLOBAL_AI_ANALYSIS_TYPE,
            status=AiAnalysisRequest.STATUS_CANCEL_REQUESTED,
            execution_token=execution_token,
        ).first()
        if request is None:
            raise GlobalAiServiceError("停止确认不可用。")
        request.status = AiAnalysisRequest.STATUS_CANCELLED
        request.finished_at = timezone.now()
        request.save(update_fields=["status", "finished_at", "updated_at"])
    return request


def _validated_usage(tokens_used, cost_estimate):
    if tokens_used is not None and (
        not isinstance(tokens_used, int) or isinstance(tokens_used, bool) or tokens_used < 0
    ):
        raise GlobalAiServiceError("Token 用量不可用。")
    if cost_estimate is None:
        return tokens_used, None
    try:
        cost = Decimal(str(cost_estimate))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise GlobalAiServiceError("费用不可用。") from exc
    if not cost.is_finite() or cost < 0:
        raise GlobalAiServiceError("费用不可用。")
    return tokens_used, cost


def complete_global_ai_request(
    *,
    request_id,
    execution_token,
    result_text,
    result_json=None,
    tokens_used=None,
    cost_estimate=None,
):
    tokens_used, cost_estimate = _validated_usage(tokens_used, cost_estimate)
    late_result = False
    with transaction.atomic():
        request = AiAnalysisRequest.objects.select_for_update().filter(
            pk=request_id,
            module=GLOBAL_AI_MODULE,
            analysis_type=GLOBAL_AI_ANALYSIS_TYPE,
        ).first()
        if request is None or request.execution_token != execution_token:
            raise GlobalAiServiceError("执行结果不可用。")
        if request.status == AiAnalysisRequest.STATUS_CANCEL_REQUESTED:
            request.status = AiAnalysisRequest.STATUS_CANCELLED
            request.finished_at = timezone.now()
            request.save(update_fields=["status", "finished_at", "updated_at"])
            late_result = True
        elif request.status != AiAnalysisRequest.STATUS_RUNNING:
            raise GlobalAiServiceError("执行结果不可用。")
        else:
            AiAnalysisResult.objects.create(
                request=request,
                result_text=result_text or "",
                result_json=dict(result_json or {}),
                tokens_used=tokens_used,
                cost_estimate=cost_estimate,
            )
            request.status = AiAnalysisRequest.STATUS_SUCCESS
            request.finished_at = timezone.now()
            request.save(update_fields=["status", "finished_at", "updated_at"])
    if late_result:
        raise GlobalAiServiceError("任务已停止，迟到结果未采纳。")
    return request


def mark_global_ai_request_unknown(*, request_id, execution_token):
    with transaction.atomic():
        request = AiAnalysisRequest.objects.select_for_update().filter(
            pk=request_id,
            module=GLOBAL_AI_MODULE,
            analysis_type=GLOBAL_AI_ANALYSIS_TYPE,
            status=AiAnalysisRequest.STATUS_RUNNING,
            execution_token=execution_token,
        ).first()
        if request is None:
            raise GlobalAiServiceError("请求不可用。")
        request.status = AiAnalysisRequest.STATUS_UNKNOWN
        request.finished_at = timezone.now()
        request.error_message = "服务端结果未知；不会自动重试。"
        request.save(update_fields=["status", "finished_at", "error_message", "updated_at"])
    return request
