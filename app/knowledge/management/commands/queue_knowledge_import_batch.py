from django.core.management.base import BaseCommand, CommandError

from knowledge.models import KnowledgeImportBatch, KnowledgeJob
from knowledge.services import queue_knowledge_job


class Command(BaseCommand):
    help = "在人工检查预览后创建正式导入或安全回滚任务。"

    def add_arguments(self, parser):
        parser.add_argument("--batch-id", type=int, required=True)
        parser.add_argument("--action", choices=["import", "rollback"], required=True)

    def handle(self, *args, **options):
        try:
            batch = KnowledgeImportBatch.objects.select_related(
                "family",
                "source",
                "requested_by",
            ).get(pk=options["batch_id"])
        except KnowledgeImportBatch.DoesNotExist as exc:
            raise CommandError("导入批次不存在。") from exc
        if options["action"] == "import":
            if batch.status not in {
                KnowledgeImportBatch.STATUS_PREVIEW_READY,
                KnowledgeImportBatch.STATUS_PARTIAL,
            } or (
                batch.status == KnowledgeImportBatch.STATUS_PARTIAL
                and batch.rolled_back_at
            ):
                raise CommandError("批次尚未完成预览且不处于部分失败状态，不能导入。")
            job_type = KnowledgeJob.TYPE_IMPORT_BATCH
        else:
            if batch.status not in {
                KnowledgeImportBatch.STATUS_COMPLETED,
                KnowledgeImportBatch.STATUS_PARTIAL,
            }:
                raise CommandError("批次当前不能回滚。")
            job_type = KnowledgeJob.TYPE_ROLLBACK_IMPORT
        job, created = queue_knowledge_job(
            family=batch.family,
            source=batch.source,
            requested_by=batch.requested_by,
            job_type=job_type,
            parameters={"batch_id": batch.pk},
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"queued job={job.pk} batch={batch.pk} created={str(created).lower()}"
            )
        )
