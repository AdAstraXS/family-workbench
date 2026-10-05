import json
import sys
from datetime import timedelta
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from family_core.household import get_household_family
from monitoring.models import CollectorState, UsageRecord
from monitoring.balances import ensure_accounts, sync_due_accounts
from monitoring.collection import ingest_host


class Command(BaseCommand):
    help='采集官方余额及 NAS 只读指标；不发起模型、转写或下载任务。'

    def add_arguments(self,parser):
        parser.add_argument('--host-json',action='store_true')

    def handle(self,*args,**options):
        state,_=CollectorState.objects.get_or_create(key='main')
        state.started_at=timezone.now(); state.status='running'; state.save()
        try:
            if options['host_json']:
                raw=sys.stdin.read(65537)
                if len(raw)>65536: raise ValueError('oversized host data')
                ingest_host(json.loads(raw))
            family=get_household_family()
            if family: ensure_accounts(family)
            sync_due_accounts()
            UsageRecord.objects.filter(status='pending').exclude(vendor='ali').filter(started_at__lt=timezone.now()-timedelta(minutes=10)).update(status='unknown',outcome='interrupted')
            UsageRecord.objects.filter(status='pending',started_at__lt=timezone.now()-timedelta(hours=24)).update(status='unknown',outcome='interrupted')
            from macro.alerts import synchronize_alerts
            synchronize_alerts()
        except Exception:
            state.status='error';state.message='采集失败，请检查任务日志与连接配置。';state.finished_at=timezone.now();state.save()
            raise CommandError(state.message) from None
        state.status='ok';state.message='采集已完成';state.finished_at=timezone.now();state.save()
        self.stdout.write('监控采集完成。')
