from datetime import timedelta
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from intelligence.program_models import ProgramEntry, ProgramSubscription
from intelligence.program_processing import process_entry
from intelligence.program_sources import collect_subscription, ProgramError
from family_core.models import Family


class Command(BaseCommand):
    help = '检查精选订阅并处理文字稿、转写和 AI 整理。建议 DSM 每 5 分钟运行，来源每小时检查。'

    def add_arguments(self, parser):
        parser.add_argument('--family-id', type=int, required=True)
        parser.add_argument('--max-steps', type=int, default=12)
        parser.add_argument('--collect-only', action='store_true')

    def handle(self, *args, **options):
        family_id, max_steps = options['family_id'], options['max_steps']
        if not Family.objects.filter(pk=family_id).exists() or not 1 <= max_steps <= 50:
            raise CommandError('家庭不存在或处理步数不在 1–50 范围内。')
        failures, collected, steps = 0, 0, 0
        # Expiring transport files is independent of whether subscriptions are paused.
        for entry in ProgramEntry.objects.filter(subscription__family_id=family_id, audio_expires_at__lte=timezone.now()).exclude(audio_file=''):
            entry.audio_file.delete(save=False)
            entry.audio_expires_at = None
            entry.save(update_fields=['audio_file', 'audio_expires_at'])
        for sub in ProgramSubscription.objects.filter(family_id=family_id, enabled=True):
            if sub.last_checked_at and sub.last_checked_at > timezone.now() - timedelta(hours=1):
                continue
            try:
                collected += collect_subscription(sub)
            except ProgramError:
                failures += 1
                self.stderr.write(f'订阅 {sub.code} 抓取失败，详情见管理订阅页面。')
        if not options['collect_only']:
            entries = ProgramEntry.objects.filter(subscription__family_id=family_id, subscription__enabled=True,
                requested=True).exclude(state__in=['ready', 'failed', 'uncertain']).order_by('updated_at', 'pk')
            for entry in list(entries[:max_steps]):
                for _ in range(min(3, max_steps - steps)):
                    succeeded = process_entry(entry.pk)
                    entry.refresh_from_db()
                    if succeeded:
                        steps += 1
                    elif entry.state in ['failed', 'uncertain']:
                        failures += 1
                    if not succeeded or entry.state not in ['text_ready', 'summarizing']:
                        break
                if steps >= max_steps:
                    break
        self.stdout.write(f'新增 {collected} 篇，处理 {steps} 步，失败 {failures} 项。')
        if failures:
            raise CommandError('部分任务失败，请查看精选订阅中的处理状态。')
