from family_core.job_runtime import BoundedJobCommand
from django.core.management.base import BaseCommand, CommandError
from investment_research.question_ai import run


class Command(BoundedJobCommand):
    help = 'Run a single frozen question suggestion or tracking request; no automatic retry.'

    def add_arguments(self, parser):
        parser.add_argument('request_id', type=int)

    def handle(self, *args, **options):
        if not run(options['request_id']):
            raise CommandError('问题分析未完成；请查看网页运行记录。')
