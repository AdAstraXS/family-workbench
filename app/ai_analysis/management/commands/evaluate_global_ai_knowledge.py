import json
from decimal import Decimal
from uuid import uuid4

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from family_core.models import FamilyMember
from knowledge.models import KnowledgeSource, KnowledgeVisibility

from ai_analysis.global_ai_jobs import provider_configuration, run_global_ai_request
from ai_analysis.global_ai_services import (
    append_conversation_message,
    create_conversation,
    submit_global_ai_request,
)
from ai_analysis.models import (
    AiAnalysisRequest,
    AiAnalysisResult,
    AiConversationMessage,
    AiOutboundAuthorization,
)


CASES = (
    (
        "K01",
        "请重新检索已授权的正式知识资料，总结交易纪律的核心原则，并列出最重要的三条。",
        True,
    ),
    (
        "K02",
        "请重新检索已授权的正式知识资料，资料中对仓位管理和单笔风险控制有哪些建议？",
        True,
    ),
    (
        "K03",
        "请重新检索已授权的正式知识资料，资料如何看待止损、认错和退出一笔错误交易？",
        True,
    ),
    (
        "K04",
        "请重新检索已授权的正式知识资料，资料中有哪些关于耐心等待和避免频繁交易的观点？",
        True,
    ),
    (
        "K05",
        "请重新检索已授权的正式知识资料，其中是否明确讨论过量子计算机采购流程？如果没有，请直接说明没有找到相关资料。",
        False,
    ),
)


class Command(BaseCommand):
    help = "用当前生产模型运行 5 个知识检索问题；会产生真实云端调用和一段可见对话。"

    def add_arguments(self, parser):
        parser.add_argument("--member-id", type=int, required=True, help="发起验收的家庭成员 ID")
        parser.add_argument(
            "--confirm-cloud-run",
            action="store_true",
            help="确认创建可见对话并调用当前云端模型 5 次",
        )
        parser.add_argument("--json", action="store_true", help="输出 JSON 验收摘要")

    def handle(self, *args, **options):
        if not options["confirm_cloud_run"]:
            raise CommandError("必须明确添加 --confirm-cloud-run；本命令会调用云端模型 5 次。")
        member = FamilyMember.objects.filter(pk=options["member_id"], is_active=True).first()
        if member is None:
            raise CommandError("家庭成员不存在或已停用。")
        provider, config = provider_configuration()
        grants = set(
            AiOutboundAuthorization.objects.filter(
                family=member.family,
                member=member,
                provider=provider,
                is_allowed=True,
                data_type__in=[
                    AiOutboundAuthorization.DATA_CONVERSATION,
                    AiOutboundAuthorization.DATA_KNOWLEDGE,
                ],
            ).values_list("data_type", flat=True)
        )
        required = {
            AiOutboundAuthorization.DATA_CONVERSATION,
            AiOutboundAuthorization.DATA_KNOWLEDGE,
        }
        if grants != required:
            raise CommandError("该成员尚未同时授权“对话”和“知识正文”。")
        source_count = KnowledgeSource.objects.filter(
            family=member.family,
            allow_cloud_ai=True,
        ).filter(
            Q(owner=member) | Q(visibility=KnowledgeVisibility.FAMILY)
        ).count()
        if not source_count:
            raise CommandError("当前成员没有可发送给云端 AI 的知识来源。")
        if AiAnalysisRequest.objects.filter(
            family=member.family,
            member=member,
            provider=provider,
            module="global_ai",
            status__in=["pending", "running", "cancel_requested"],
        ).exists():
            raise CommandError("该成员已有一条问题正在处理，请先等待或结束异常状态。")

        run_id = uuid4().hex
        conversation = create_conversation(
            member,
            title=f"知识检索验收 {timezone.localtime():%Y-%m-%d %H:%M}",
        )
        rows = []
        total_tokens = 0
        total_cost = Decimal("0")
        for case_id, prompt, expect_evidence in CASES:
            with transaction.atomic():
                request, created = submit_global_ai_request(
                    member,
                    conversation_id=conversation.pk,
                    idempotency_key=f"knowledge-eval-{run_id}-{case_id}",
                    prompt=prompt,
                    provider=provider,
                    scope={"config_fingerprint": config["fingerprint"], "evaluation_case": case_id},
                )
                if not created:
                    raise CommandError(f"{case_id} 未能建立新的验收请求。")
                append_conversation_message(
                    member,
                    conversation_id=conversation.pk,
                    role=AiConversationMessage.ROLE_USER,
                    content=prompt,
                )
            run_global_ai_request(request.pk)
            request.refresh_from_db()
            result = AiAnalysisResult.objects.filter(request=request).first()
            answer = None
            if result:
                answer = AiConversationMessage.objects.filter(
                    pk=result.result_json.get("message_id"),
                    conversation=conversation,
                    role=AiConversationMessage.ROLE_ASSISTANT,
                ).first()
                total_tokens += result.tokens_used or 0
                total_cost += result.cost_estimate or Decimal("0")
            evidence = [
                ref for ref in (answer.evidence_refs if answer else [])
                if isinstance(ref, dict) and ref.get("kind") == "knowledge"
            ]
            evidence_ok = bool(evidence) if expect_evidence else not evidence
            rows.append({
                "case_id": case_id,
                "status": request.status,
                "expected_knowledge_evidence": expect_evidence,
                "knowledge_evidence_count": len(evidence),
                "evidence_ok": evidence_ok,
                "tokens": result.tokens_used if result else None,
                "cost_usd": str(result.cost_estimate) if result and result.cost_estimate is not None else None,
                "answer_preview": (answer.content[:240] if answer else ""),
                "error": request.error_message,
            })

        summary = {
            "conversation_id": conversation.pk,
            "conversation_title": conversation.title,
            "provider": provider.model_name,
            "source_count": source_count,
            "technical_successes": sum(row["status"] == AiAnalysisRequest.STATUS_SUCCESS for row in rows),
            "evidence_checks_passed": sum(row["evidence_ok"] for row in rows),
            "total_tokens": total_tokens,
            "total_cost_usd": str(total_cost),
            "cases": rows,
        }
        if options["json"]:
            self.stdout.write(json.dumps(summary, ensure_ascii=False, indent=2))
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f"验收对话 #{conversation.pk}：技术成功 {summary['technical_successes']}/5，"
                    f"证据检查 {summary['evidence_checks_passed']}/5，Token {total_tokens}，"
                    f"费用 ${total_cost}。"
                )
            )
            for row in rows:
                self.stdout.write(
                    f"{row['case_id']} {row['status']}：知识引用 {row['knowledge_evidence_count']}；"
                    f"Token {row['tokens'] or '未知'}；费用 ${row['cost_usd'] or '未知'}\n"
                    f"  {row['answer_preview']}"
                )
        if summary["technical_successes"] != len(CASES):
            raise CommandError("知识检索验收存在模型调用失败，请查看上方结果；未自动重试。")
