from django.core.management.base import BaseCommand

from knowledge.models import KnowledgeJob, KnowledgeSource
from knowledge.services import queue_knowledge_job


class Command(BaseCommand):
    help = "为所有已启用且连接正常的 OneNote 来源创建同步任务。"

    def add_arguments(self, parser):
        parser.add_argument(
            "--full-reconcile",
            action="store_true",
            help="本轮同时对账来源已删除页面。",
        )

    def handle(self, *args, **options):
        created_count = 0
        existing_count = 0
        sources = KnowledgeSource.objects.filter(
            kind=KnowledgeSource.KIND_ONENOTE,
            is_enabled=True,
            status__in=[
                KnowledgeSource.STATUS_ACTIVE,
                KnowledgeSource.STATUS_ERROR,
            ],
            connection__status="active",
        ).select_related("family", "owner")
        for source in sources:
            _, created = queue_knowledge_job(
                family=source.family,
                source=source,
                requested_by=source.owner,
                job_type=KnowledgeJob.TYPE_SYNC_SOURCE,
                parameters={"full_reconcile": options["full_reconcile"]},
            )
            if created:
                created_count += 1
            else:
                existing_count += 1
        self.stdout.write(
            self.style.SUCCESS(
                f"queued={created_count} already_active={existing_count}"
            )
        )
