from family_core.job_runtime import BoundedJobCommand
from django.core.management.base import BaseCommand, CommandError
from investment_research.preparation import run


class Command(BoundedJobCommand):
    help = "Run one explicitly requested company introduction."

    def add_arguments(self, parser):
        parser.add_argument("request_id", type=int)

    def handle(self, *args, **options):
        if run(options["request_id"]) is False:
            raise CommandError("初识报告未完成；原因已保存在该研究档案中。")
