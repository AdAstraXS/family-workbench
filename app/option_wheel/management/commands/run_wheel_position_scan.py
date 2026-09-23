from uuid import UUID

from django.core.management.base import BaseCommand, CommandError

from option_wheel.position_scan_jobs import run_job


class Command(BaseCommand):
    help = "Run one queued, read-only option position comparison."

    def add_arguments(self, parser):
        parser.add_argument("job_id")

    def handle(self, *args, **options):
        try:
            job_id = UUID(options["job_id"])
        except ValueError:
            raise CommandError("Invalid job ID.") from None
        run_job(job_id)
