from django.core.management.base import BaseCommand, CommandError

from knowledge.models import KnowledgeJob
from knowledge.services import claim_next_job, process_job


class Command(BaseCommand):
    help = "领取并处理知识底座后台任务；失败或部分成功时返回非零状态。"

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit",
            type=int,
            default=5,
            help="本次最多处理多少个排队任务，默认 5。",
        )

    def handle(self, *args, **options):
        limit = max(1, min(options["limit"], 100))
        processed = []
        for _ in range(limit):
            job = claim_next_job()
            if job is None:
                break
            self.stdout.write(
                f"processing job={job.pk} type={job.job_type} source={job.source_id}"
            )
            process_job(job)
            job.refresh_from_db()
            processed.append(job)
            self.stdout.write(
                f"finished job={job.pk} status={job.status} "
                f"new={job.success_count} updated={job.updated_count} "
                f"skipped={job.skipped_count} failed={job.failed_count}"
            )

        if not processed:
            self.stdout.write("no pending knowledge jobs")
            return

        non_success = [
            job
            for job in processed
            if job.status
            in {
                KnowledgeJob.STATUS_PARTIAL,
                KnowledgeJob.STATUS_FAILED,
                KnowledgeJob.STATUS_SOURCE_UNAVAILABLE,
            }
        ]
        if non_success:
            ids = ", ".join(str(job.pk) for job in non_success)
            raise CommandError(f"knowledge jobs require attention: {ids}")
