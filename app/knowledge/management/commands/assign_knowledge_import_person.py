from django.core.management.base import BaseCommand, CommandError

from knowledge.imports import KnowledgeImportError, assign_import_batch_person
from knowledge.models import KnowledgeImportBatch


class Command(BaseCommand):
    help = "统一导入批次的归属人物，并保留逐篇 HTML 原始署名。"

    def add_arguments(self, parser):
        parser.add_argument("--batch-id", type=int, required=True)
        parser.add_argument("--person-name", required=True)

    def handle(self, *args, **options):
        try:
            batch = KnowledgeImportBatch.objects.get(pk=options["batch_id"])
            result = assign_import_batch_person(batch, options["person_name"])
        except KnowledgeImportBatch.DoesNotExist as exc:
            raise CommandError("导入批次不存在。") from exc
        except KnowledgeImportError as exc:
            raise CommandError(str(exc)) from exc

        aliases = "、".join(result["source_aliases"]) or "无原始署名"
        self.stdout.write(
            self.style.SUCCESS(
                f"batch={batch.pk} person={result['person_name']} "
                f"items={result['item_count']} documents={result['document_count']} "
                f"source_aliases={aliases}"
            )
        )
