from uuid import UUID
from django.core.management.base import CommandError
from family_core.job_runtime import BoundedJobCommand
from portfolio.fund_jobs import run_job


class Command(BoundedJobCommand):
    help = "执行一条已排队的基金公共数据更新任务。"

    def add_arguments(self, parser):
        parser.add_argument("job_id")

    def handle(self, *args, **options):
        try:
            job_id = UUID(options["job_id"])
        except ValueError:
            raise CommandError("任务编号无效。") from None
        run_job(job_id)

