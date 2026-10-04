from family_core.job_runtime import BoundedJobCommand
from django.core.management.base import BaseCommand, CommandError
from investment_research.thesis_analysis import run_thesis_analysis
from investment_research.research_ai import ResearchAiError


class Command(BoundedJobCommand):
    help = "Run one explicitly requested research analysis using its frozen input."

    def add_arguments(self, parser):
        parser.add_argument("request_id", type=int)

    def handle(self, *args, **options):
        try:
            run_thesis_analysis(options["request_id"])
        except ResearchAiError as exc:
            raise CommandError(str(exc)) from exc
