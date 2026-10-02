from django.core.management.base import BaseCommand
from django.utils import timezone
from investment_research.company_jobs import execute_item
from investment_research.models import CompanyAcquisitionJob
from investment_research.material_store import record_failure


class Command(BaseCommand):
    help = "公司资料任务的单项隔离执行器。"
    def add_arguments(self, parser):
        parser.add_argument("job_id", type=int)
        parser.add_argument("key")
    def handle(self, *args, **options):
        job = CompanyAcquisitionJob.objects.select_related("dossier__security").get(pk=options["job_id"])
        key = options["key"]
        if job.status != "running" or job.expires_at <= timezone.now() or not job.items or job.items[-1]["key"] != key:
            return
        try:
            message = execute_item(job, key)
            status = "success"
        except Exception as exc:
            status = "failed"
            message = str(exc)[:300] if isinstance(exc, ValueError) else "来源暂不可用或解析失败，请单项重试；原有资料已保留。"
            if key.startswith("sec:"):
                kind = "sec_document"
            else:
                kind = key
            record_failure(job.dossier.security, key, kind, job.items[-1]["title"], message)
        job.items[-1].update(status=status, message=message)
        job.save(update_fields=["items", "updated_at"])
