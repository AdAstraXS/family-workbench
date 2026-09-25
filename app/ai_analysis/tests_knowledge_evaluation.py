import json
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from family_core.models import Family, FamilyMember
from knowledge.models import KnowledgeDocument, KnowledgeRevision, KnowledgeSource, KnowledgeVisibility

from .global_ai_services import claim_global_ai_request, complete_global_ai_request
from .models import AiAnalysisRequest, AiConversation, AiOutboundAuthorization, AiProvider


class KnowledgeEvaluationCommandTests(TestCase):
    def setUp(self):
        family = Family.objects.create(name="验收测试家庭", base_currency="CNY")
        user = get_user_model().objects.create_user(username="knowledge-evaluator")
        self.member = FamilyMember.objects.create(
            family=family,
            user=user,
            display_name="验收成员",
            role=FamilyMember.ROLE_ADMIN,
        )
        self.provider = AiProvider.objects.create(
            name="DeepSeek V4-Pro",
            provider_type="openai_compatible",
            model_name="deepseek-v4-pro",
            base_url="https://api.deepseek.com",
            execution_location=AiProvider.LOCATION_CLOUD,
            is_active=True,
        )
        for data_type in (
            AiOutboundAuthorization.DATA_CONVERSATION,
            AiOutboundAuthorization.DATA_KNOWLEDGE,
        ):
            AiOutboundAuthorization.objects.create(
                family=family,
                member=self.member,
                provider=self.provider,
                data_type=data_type,
                is_allowed=True,
            )
        source = KnowledgeSource.objects.create(
            family=family,
            owner=self.member,
            key="evaluation-source",
            kind=KnowledgeSource.KIND_INTERNAL_NOTES,
            name="投资得失",
            visibility=KnowledgeVisibility.FAMILY,
            allow_cloud_ai=True,
        )
        self.document = KnowledgeDocument.objects.create(
            family=family,
            source=source,
            owner=self.member,
            external_id="evaluation-document",
            title="交易纪律",
            visibility=KnowledgeVisibility.FAMILY,
            knowledge_status=KnowledgeDocument.KNOWLEDGE_INCLUDED,
        )
        self.revision = KnowledgeRevision.objects.create(
            document=self.document,
            revision_number=1,
            content_hash="e" * 64,
            plain_text="控制仓位，遵守止损纪律。",
        )
        self.document.current_revision = self.revision
        self.document.save(update_fields=["current_revision", "updated_at"])

    def test_requires_explicit_cloud_confirmation(self):
        with self.assertRaisesRegex(CommandError, "confirm-cloud-run"):
            call_command("evaluate_global_ai_knowledge", member_id=self.member.pk)
        self.assertEqual(AiConversation.objects.count(), 0)

    def test_runs_five_cases_and_reports_usage(self):
        def fake_run(request_id):
            request = AiAnalysisRequest.objects.get(pk=request_id)
            token = claim_global_ai_request(request_id=request_id)
            case_id = request.scope["evaluation_case"]
            refs = [] if case_id == "K05" else [{
                "kind": "knowledge",
                "document_id": self.document.pk,
                "revision_id": self.revision.pk,
            }]
            complete_global_ai_request(
                request_id=request_id,
                execution_token=token,
                result_text=f"{case_id} 验收回答",
                tokens_used=10,
                cost_estimate="0.001",
                message_data_types=[AiOutboundAuthorization.DATA_KNOWLEDGE],
                evidence_refs=refs,
            )

        output = StringIO()
        config = {"fingerprint": "f" * 64}
        with patch(
            "ai_analysis.management.commands.evaluate_global_ai_knowledge.provider_configuration",
            return_value=(self.provider, config),
        ), patch(
            "ai_analysis.management.commands.evaluate_global_ai_knowledge.run_global_ai_request",
            side_effect=fake_run,
        ):
            call_command(
                "evaluate_global_ai_knowledge",
                member_id=self.member.pk,
                confirm_cloud_run=True,
                json=True,
                stdout=output,
            )

        summary = json.loads(output.getvalue())
        self.assertEqual(summary["technical_successes"], 5)
        self.assertEqual(summary["evidence_checks_passed"], 5)
        self.assertEqual(summary["total_tokens"], 50)
        self.assertEqual(summary["total_cost_usd"], "0.005000")
        self.assertEqual(AiAnalysisRequest.objects.count(), 5)
        self.assertEqual(AiConversation.objects.count(), 1)
