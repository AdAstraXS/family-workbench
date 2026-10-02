"""Record each outbound attempt, before result validation; never store prompts or responses."""
import json
import logging
from django.db import transaction
from django.utils import timezone
from .models import UsageRecord
from .pricing import cost_for, decimal_value, rate_snapshot, vendor_for

logger = logging.getLogger(__name__)


def record_download(entry, size, mime):
    try:
        from .models import DownloadRecord
        with transaction.atomic():
            DownloadRecord.objects.create(family_id=entry.subscription.family_id, entry_id=entry.pk,
                source=entry.subscription.kind or entry.subscription.code, file_bytes=size, media_type=mime)
    except Exception:
        logger.error('Monitoring could not persist download metrics.')


def integer(value, maximum=10**12):
    return value if type(value) is int and 0 <= value <= maximum else None


def start(provider, module, family_id, source='', *, audio=False):
    try:
        with transaction.atomic():
            if not family_id:
                from family_core.household import get_household_family
                family = get_household_family()
                family_id = family.pk if family else None
            if not family_id: return None
            return UsageRecord.objects.create(family_id=family_id, provider=provider,
                vendor='ali' if audio else vendor_for(provider), model_name='fun-asr-2025-11-07' if audio else provider.model_name,
                module=module, source_ref=str(source)[:160], price_snapshot=rate_snapshot(provider,audio=audio))
    except Exception:
        logger.error('Monitoring could not persist call start; usage may be missing.')
        return None


def finish(record, response=None, *, failed=False, pending=False):
    if record is None: return
    try:
        if isinstance(response,(bytes,str)):
            try: response=json.loads(response)
            except (ValueError,TypeError,UnicodeError): response={}
        response=response if isinstance(response,dict) else {}
        usage=response.get('usage')
        usage=usage if isinstance(usage,dict) else {}
        record.finished_at=None if pending else timezone.now()
        record.elapsed_ms=min(int((timezone.now()-record.started_at).total_seconds()*1000),2**31-1)
        record.outcome='request_error' if failed else ('pending' if pending else 'response_received')
        request_id=response.get('id') or response.get('request_id') or ''
        if record.vendor=='ali': request_id=(response.get('output') or {}).get('task_id') or record.vendor_request_id
        if isinstance(request_id,str): record.vendor_request_id=request_id[:160]
        record.input_tokens=integer(usage.get('prompt_tokens',usage.get('input_tokens')))
        record.output_tokens=integer(usage.get('completion_tokens',usage.get('output_tokens')))
        details=usage.get('prompt_tokens_details') or usage.get('input_tokens_details') or {}
        details=details if isinstance(details,dict) else {}
        record.cached_tokens=integer(usage.get('prompt_cache_hit_tokens',details.get('cached_tokens')))
        if record.input_tokens is not None and record.cached_tokens is not None and record.cached_tokens>record.input_tokens:
            record.cached_tokens=None
        record.total_tokens=integer(usage.get('total_tokens'))
        if record.input_tokens is not None and record.output_tokens is not None:
            record.total_tokens=record.input_tokens+record.output_tokens
        if record.vendor=='ali': record.audio_seconds=decimal_value(usage.get('duration'))
        record.status='pending' if pending else ('confirmed' if record.total_tokens is not None or record.audio_seconds is not None else 'unknown')
        record.cost_cny=cost_for(record)
        with transaction.atomic(): record.save()
    except Exception:
        logger.error('Monitoring could not persist call result; usage may be unconfirmed.')


def tracked_call(call, *, provider, module, family_id=None, source='', audio=False):
    record=start(provider,module,family_id,source,audio=audio)
    try:
        result=call()
    except BaseException:
        finish(record,failed=True)
        raise
    finish(record,result,pending=audio and isinstance(result,dict) and (result.get('output') or {}).get('task_status') in {'PENDING','RUNNING'})
    return result


def capture_asr(task_id, family_id, response):
    try:
        status=(response.get('output') or {}).get('task_status')
        if status in {'PENDING','RUNNING'}: return
        with transaction.atomic():
            record=UsageRecord.objects.select_for_update().filter(vendor='ali',family_id=family_id,vendor_request_id=task_id).order_by('-started_at').first()
            # Existing tasks submitted before monitoring are not silently backfilled.
            if record and record.status!='confirmed': finish(record,response,failed=status!='SUCCEEDED')
    except Exception:
        logger.error('Monitoring could not persist transcription metrics.')


def read_response(opener, request, timeout, limit):
    with opener(request,timeout=timeout) as response:
        return response.read(limit)
