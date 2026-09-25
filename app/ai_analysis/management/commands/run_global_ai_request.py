from django.core.management.base import BaseCommand, CommandError

from ai_analysis.global_ai_jobs import run_global_ai_request
from ai_analysis.models import AiAnalysisRequest


class Command(BaseCommand):
    help = "处理一份已授权的全局 AI 请求。"

    def add_arguments(self, parser):
        parser.add_argument("request_id", type=int)

    def handle(self, request_id, **options):
        run_global_ai_request(request_id)
        status = AiAnalysisRequest.objects.filter(pk=request_id).values_list("status", flat=True).first()
        if status not in {AiAnalysisRequest.STATUS_SUCCESS, AiAnalysisRequest.STATUS_CANCELLED}:
            raise CommandError("AI 请求未完成，请在 AI 助手页面核对状态。")
