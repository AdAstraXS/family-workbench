from django.core.management.base import BaseCommand, CommandError
from reading.ai import expire_stalled_jobs, process_job
from reading.models import ReadingAiJob


class Command(BaseCommand):
    help="处理成员逐次确认过的阅读 AI 任务；不会扫描整书或自动重试付费请求。"

    def add_arguments(self,parser):
        parser.add_argument("--limit",type=int,default=2)

    def handle(self,*args,**options):
        limit=options["limit"]
        if not 1<=limit<=20:raise CommandError("limit 须为 1 至 20。")
        expired=expire_stalled_jobs()
        ids=list(ReadingAiJob.objects.filter(status="queued").order_by("created_at").values_list("pk",flat=True)[:limit])
        for pk in ids:process_job(pk)
        failed=ReadingAiJob.objects.filter(pk__in=ids,status="failed").count()+expired
        self.stdout.write(f"本轮选取 {len(ids)} 个任务，失败或中断 {failed} 个。")
        if failed:raise CommandError("阅读 AI 有失败任务，请在图书详情查看原因。")
