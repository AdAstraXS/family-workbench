"""Prepare one public-source event brief when new official material is archived."""

from django.core.management.base import BaseCommand, CommandError

from investment_research.models import ResearchDossier
from investment_research.next_day_digest import generate_next_day_digest, pending_sources
from investment_research.research_ai import ResearchAiError


class Command(BaseCommand):
    help = "Generate source-cited, public-material next-day research briefs."

    def add_arguments(self, parser):
        parser.add_argument("--dossier-id", type=int)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        from django.db.models import Q
        from investment_research.question_ai import automatic_check
        dossiers = ResearchDossier.objects.filter(Q(current_revision__isnull=False) | Q(question_workflow=True))
        if options["dossier_id"]:
            dossiers = dossiers.filter(pk=options["dossier_id"])
            if not dossiers.exists():
                raise CommandError("研究档案不存在或尚无正式判断。")
        generated = pending = failed = 0
        for dossier in dossiers.iterator():
            if dossier.question_workflow:
                if options['dry_run']:
                    continue
                try:
                    job = automatic_check(dossier)
                    if job and job.status == 'pending':
                        pending += 1
                        self.stdout.write(f'研究档案 {dossier.pk}：问题核查任务 {job.pk} 已排队。')
                except ResearchAiError as exc:
                    failed += 1
                    self.stderr.write(f'研究档案 {dossier.pk}：{exc}')
                continue
            if not pending_sources(dossier):
                continue
            pending += 1
            if options["dry_run"]:
                continue
            try:
                result = generate_next_day_digest(dossier.pk)
            except ResearchAiError as exc:
                failed += 1
                self.stderr.write(f"研究档案 {dossier.pk}：{exc}")
            else:
                if result:
                    generated += 1
                    self.stdout.write(f"研究档案 {dossier.pk}：生成简报 {result.pk}")
        self.stdout.write(f"待处理 {pending}，已生成 {generated}，失败 {failed}。")
        if failed:
            raise CommandError("部分次日跟踪简报生成失败。")
