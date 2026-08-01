from django.core.management.base import BaseCommand, CommandError

from family_core.models import Family
from knowledge.search import rebuild_family_search


class Command(BaseCommand):
    help = "从知识原文和随手记权威数据重建可删除的知识搜索投影。"

    def add_arguments(self, parser):
        parser.add_argument("--family-id", type=int)

    def handle(self, *args, **options):
        families = Family.objects.order_by("pk")
        if options["family_id"]:
            families = families.filter(pk=options["family_id"])
            if not families.exists():
                raise CommandError("指定家庭不存在。")
        total_notes = 0
        total_documents = 0
        for family in families:
            result = rebuild_family_search(family)
            total_notes += result["notes"]
            total_documents += result["documents"]
            self.stdout.write(
                f"family={family.pk} notes={result['notes']} "
                f"documents={result['documents']}"
            )
        self.stdout.write(
            self.style.SUCCESS(
                f"rebuilt notes={total_notes} documents={total_documents}"
            )
        )
