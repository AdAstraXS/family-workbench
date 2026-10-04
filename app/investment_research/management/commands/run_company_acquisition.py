from family_core.job_runtime import BoundedJobCommand
from django.core.management.base import BaseCommand
from investment_research.company_jobs import run


class Command(BoundedJobCommand):
    help = "执行公司基础资料获取任务；每项独立限时，结果保存在资料页。"
    def add_arguments(self, parser):
        parser.add_argument("job_id", type=int)
    def handle(self, *args, **options):
        run(options["job_id"])
