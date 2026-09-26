from django.core.management.base import BaseCommand, CommandError

from ai_analysis.models import AiAnalysisRequest
from option_wheel.advice_jobs import MODULE
from option_wheel.screen_advice import SCHEMA
from option_wheel.screen_advice_jobs import run_screen_advice


class Command(BaseCommand):
    help = "处理一次用户明确开启的冻结合约 AI 建议批次。"

    def add_arguments(self, parser):
        parser.add_argument("request_id", type=int)

    def handle(self, *args, **options):
        request_id = options["request_id"]
        run_screen_advice(request_id)
        if not AiAnalysisRequest.objects.filter(
                pk=request_id, module=MODULE, analysis_type=SCHEMA, status="success").exists():
            raise CommandError("本批 AI 建议未生成；请查看任务记录。")
