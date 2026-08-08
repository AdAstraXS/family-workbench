from django.core.management.base import BaseCommand, CommandError

from family_core.models import FamilyMember
from knowledge.imports import KnowledgeImportError, stage_html_directory_batch
from knowledge.models import KnowledgeJob, KnowledgeVisibility
from knowledge.services import queue_knowledge_job


class Command(BaseCommand):
    help = "从受控目录选取 HTML 和配套图片，保存原始导入包并创建只读预览任务。"

    def add_arguments(self, parser):
        parser.add_argument("--member-id", type=int, required=True)
        parser.add_argument("--root", required=True)
        parser.add_argument(
            "--file",
            action="append",
            required=True,
            help="相对于 --root 的 HTML 路径；可重复指定。",
        )
        parser.add_argument("--source-name", default="微信公众号 · 金渐成")
        parser.add_argument("--source-key", default="html-import:wechat-jinjiancheng")
        parser.add_argument("--category", default="公众号归档")
        parser.add_argument(
            "--visibility",
            choices=[choice[0] for choice in KnowledgeVisibility.choices],
            default=KnowledgeVisibility.FAMILY,
        )

    def handle(self, *args, **options):
        try:
            member = FamilyMember.objects.select_related("family").get(
                pk=options["member_id"],
                is_active=True,
            )
            batch = stage_html_directory_batch(
                root=options["root"],
                html_files=options["file"],
                family=member.family,
                member=member,
                source_name=options["source_name"],
                source_key=f"{options['source_key']}:{member.pk}",
                visibility=options["visibility"],
                category=options["category"],
            )
            job, _ = queue_knowledge_job(
                family=member.family,
                source=batch.source,
                requested_by=member,
                job_type=KnowledgeJob.TYPE_PREVIEW_IMPORT,
                parameters={"batch_id": batch.pk},
            )
        except (FamilyMember.DoesNotExist, KnowledgeImportError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            self.style.SUCCESS(
                f"staged batch={batch.pk} preview_job={job.pk} files={len(options['file'])}"
            )
        )
