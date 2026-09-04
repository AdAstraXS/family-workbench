from django.core.management.base import BaseCommand, CommandError
from option_wheel.advice_jobs import run_advice, MODULE
from ai_analysis.models import AiAnalysisRequest


class Command(BaseCommand):
    help = "处理一份已授权的 DeepSeek 期权解释请求；不抓行情、不下单。"

    def add_arguments(self, parser):
        parser.add_argument("request_id", type=int)

    def handle(self, request_id, **options):
        run_advice(request_id)
        if not AiAnalysisRequest.objects.filter(pk=request_id, module=MODULE, status="success").exists():
            raise CommandError("AI 请求未完成，请在策略建议页核对状态。")
