from django.core.management.base import BaseCommand, CommandError

from knowledge.content import ONENOTE_CONVERTER_VERSION
from knowledge.models import KnowledgeDocument, KnowledgeSource
from knowledge.services import rebuild_document_normalized_content


class Command(BaseCommand):
    help = "从 NAS 原始版本与附件安全重建派生正文和搜索索引。"

    def add_arguments(self, parser):
        scope = parser.add_mutually_exclusive_group(required=True)
        scope.add_argument("--source-id", type=int)
        scope.add_argument("--document-id", type=int)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        documents = KnowledgeDocument.objects.filter(
            current_revision__isnull=False,
        ).select_related("current_revision", "source", "owner", "family")
        if options["source_id"]:
            source_id = options["source_id"]
            if not KnowledgeSource.objects.filter(pk=source_id).exists():
                raise CommandError("指定知识来源不存在。")
            documents = documents.filter(source_id=source_id)
        else:
            document_id = options["document_id"]
            documents = documents.filter(pk=document_id)
            if not documents.exists():
                raise CommandError("指定知识文档不存在或没有原始版本。")

        dry_run = options["dry_run"]
        counters = {"updated": 0, "skipped": 0, "failed": 0}
        for document in documents.iterator():
            revision = document.current_revision
            if revision.converter_version == ONENOTE_CONVERTER_VERSION:
                counters["skipped"] += 1
                continue
            try:
                result = rebuild_document_normalized_content(
                    document,
                    save=not dry_run,
                )
            except Exception as exc:
                counters["failed"] += 1
                self.stderr.write(f"document={document.pk} failed={exc}")
                continue
            counters["updated"] += int(result["changed"])
            counters["skipped"] += int(not result["changed"])
            self.stdout.write(
                f"document={document.pk} converter={ONENOTE_CONVERTER_VERSION} "
                f"text_length={result['plain_text_length']}"
            )

        action = "previewed" if dry_run else "rebuilt"
        summary = (
            f"{action} updated={counters['updated']} "
            f"skipped={counters['skipped']} failed={counters['failed']}"
        )
        if counters["failed"]:
            raise CommandError(summary)
        self.stdout.write(self.style.SUCCESS(summary))
