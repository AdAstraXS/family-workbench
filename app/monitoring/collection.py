from datetime import timedelta
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from .models import HostSample, CollectorState
from .metering import integer as usage_integer


def integer(value):
    return usage_integer(value, 2**63-1)


def timestamp(value):
    dt=parse_datetime(value) if isinstance(value,str) else None
    return dt if dt and timezone.is_aware(dt) else None


@transaction.atomic
def ingest_host(payload):
    # Serialize adjacent samples and idempotently accept repeated deliveries.
    CollectorState.objects.get_or_create(key='host')
    CollectorState.objects.select_for_update().get(key='host')
    when=timestamp(payload.get('sampled_at'))
    if when is None or abs((timezone.now()-when).total_seconds())>600: raise ValueError('invalid sample timestamp')
    existing=HostSample.objects.filter(sampled_at=when).first()
    if existing: return existing
    previous=HostSample.objects.order_by('-sampled_at').first()
    if previous and previous.sampled_at>when: raise ValueError('out of order sample')
    row=HostSample(sampled_at=when,proxy_ok=payload.get('proxy_ok') is True,
        session_id=str(payload.get('session_id',''))[:100],disk_total=integer(payload.get('disk_total')),
        disk_free=integer(payload.get('disk_free')),backup_at=timestamp(payload.get('backup_at')),
        backup_name=str(payload.get('backup_name',''))[:200],message=str(payload.get('message',''))[:200])
    row.upload_total=integer(payload.get('upload_total'))
    row.download_total=integer(payload.get('download_total'))
    row.proxy_ok=row.proxy_ok and row.upload_total is not None and row.download_total is not None
    row.log_activity={k:v for k,v in (payload.get('log_activity') or {}).items() if k in {
        'program-subscriptions','reading-jobs','knowledge-jobs','research-digest','research-sec-sync','daily-portfolio-valuation','option-wheel-watch'} and timestamp(v)}
    row.subscriptions=[]
    for item in (payload.get('subscriptions') or [])[:20]:
        if not isinstance(item,dict): continue
        clean={k:integer(item.get(k)) for k in ('upload','download','total','expire')}
        if clean['total'] is not None:
            row.subscriptions.append({'name':str(item.get('name','订阅'))[:80],**clean})
    if row.proxy_ok and previous and previous.proxy_ok:
        same_epoch=not row.session_id or not previous.session_id or row.session_id==previous.session_id
        if same_epoch and row.upload_total>=previous.upload_total and row.download_total>=previous.download_total:
            row.upload_delta=row.upload_total-previous.upload_total
            row.download_delta=row.download_total-previous.download_total
            row.gap=when-previous.sampled_at>timedelta(minutes=10)
        else:
            row.gap=True
    else:
        row.gap=True
    row.save()
    return row
