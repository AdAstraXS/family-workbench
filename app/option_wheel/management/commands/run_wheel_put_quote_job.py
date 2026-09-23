from uuid import UUID

from django.core.management.base import BaseCommand, CommandError

from option_wheel.put_quote_jobs import run_job


class Command(BaseCommand):
    help = "Run one queued, read-only Futu quote job for recorded short Puts."

    def add_arguments(self, parser):
        parser.add_argument("job_id")

    def handle(self, *args, **options):
        try:
            job_id = UUID(options["job_id"])
        except ValueError as exc:
            raise CommandError("任务编号无效。") from exc
        run_job(job_id)
