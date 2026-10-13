from django.core.management.base import CommandError
from family_core.job_runtime import BoundedJobCommand
from family_core.models import Family
from portfolio.fund_jobs import enqueue, run_job
from portfolio.fund_models import FundDcaPlan


class Command(BoundedJobCommand):
    help = "供DSM计划任务更新家庭定投基金净值、资料和披露；不修改定投金额或余额。"

    def handle(self, *args, **options):
        failed = False
        for family in Family.objects.filter(fund_dca_plans__isnull=False).distinct():
            ids = FundDcaPlan.objects.filter(family=family).values_list("fund_id", flat=True)
            job = enqueue(family, None, ids, launch=False)
            if job.status != "queued" or job.requested_by_id:
                self.stdout.write(f"family={family.pk}: 已有更新任务，跳过")
                continue
            run_job(job.pk)
            job.refresh_from_db()
            self.stdout.write(f"family={family.pk}: {job.status} {job.message}")
            failed |= job.status != "saved"
        if failed:
            raise CommandError("部分基金数据未更新，详情见基金定投页面更新记录。")
