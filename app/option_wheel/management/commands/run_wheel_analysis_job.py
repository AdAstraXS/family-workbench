from uuid import UUID
from django.core.management.base import BaseCommand, CommandError
from option_wheel.jobs import run_job
from option_wheel.models import WheelAnalysisJob


class Command(BaseCommand):
    help = "处理一份已授权的车轮实时分析任务；不创建任务，不下单。"

    def add_arguments(self, parser):
        parser.add_argument("job_id", type=UUID)

    def handle(self, job_id, **options):
        run_job(job_id)
        status = WheelAnalysisJob.objects.filter(pk=job_id).values_list("status", flat=True).first()
        if status != "saved":
            raise CommandError("任务未完成保存，请在网页任务记录中核对状态。")
