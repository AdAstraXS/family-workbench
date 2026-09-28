"""Official balance readers: no scraping, redirects, or arbitrary URLs."""
import base64
import hashlib
import hmac
import json
import os
import uuid
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode, quote
from urllib.request import Request, build_opener, HTTPRedirectHandler
from django.utils import timezone
from knowledge.crypto import decrypt_json
from .models import BalanceAccount
from .pricing import vendor_for


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs): return None


def read_json(request):
    with build_opener(NoRedirect()).open(request,timeout=12) as response:
        raw=response.read(262145)
    if len(raw)>262144: raise ValueError('oversized balance response')
    return json.loads(raw)


def ensure_accounts(family):
    from ai_analysis.models import AiProvider
    for provider in AiProvider.objects.filter(is_active=True):
        vendor=vendor_for(provider)
        if vendor=='other': continue
        env=(provider.extra_data or {}).get('api_key_env_var') or {'zhipu':'ZHIPU_API_KEY','volcano':'ARK_API_KEY'}.get(vendor,'')
        # Models using the same named credential share a balance account.
        account_key=(provider.extra_data or {}).get('monitoring_account') or env or 'default'
        BalanceAccount.objects.get_or_create(family=family,vendor=vendor,account_key=account_key,
            defaults={'label':dict(BalanceAccount._meta.get_field('vendor').choices)[vendor],'key_env':env})
    BalanceAccount.objects.get_or_create(family=family,vendor='ali',account_key='default',defaults={'label':'阿里百炼'})


def balance_value(value):
    try:
        if isinstance(value,bool) or value is None: return None
        amount=Decimal(str(value).replace(',',''))
        return amount if amount.is_finite() and abs(amount)<10**12 else None
    except (InvalidOperation,ValueError,TypeError):
        return None


def volcano_balance(credentials):
    # Fixed official billing API, following the vendor SDK V4 signing specification.
    host='open.volcengineapi.com'
    query='Action=QueryBalanceAcct&Version=2022-01-01'
    stamp=timezone.now().strftime('%Y%m%dT%H%M%SZ')
    body=b'{}'; digest=hashlib.sha256(body).hexdigest()
    headers={'Content-Type':'application/json','Host':host,'X-Date':stamp,'X-Content-Sha256':digest}
    signed='content-type;host;x-content-sha256;x-date'
    canonical_headers=''.join(k.lower()+':'+v+'\n' for k,v in sorted(headers.items(),key=lambda pair:pair[0].lower()))
    canonical='\n'.join(['POST','/',query,canonical_headers,signed,digest])
    scope=stamp[:8]+'/cn-beijing/billing/request'
    to_sign='\n'.join(['HMAC-SHA256',stamp,scope,hashlib.sha256(canonical.encode()).hexdigest()])
    key=credentials['access_key_secret'].encode()
    for part in (stamp[:8],'cn-beijing','billing','request'):
        key=hmac.new(key,part.encode(),hashlib.sha256).digest()
    signature=hmac.new(key,to_sign.encode(),hashlib.sha256).hexdigest()
    headers['Authorization']='HMAC-SHA256 Credential='+credentials['access_key_id']+'/'+scope+', SignedHeaders='+signed+', Signature='+signature
    payload=read_json(Request('https://'+host+'/?'+query,data=body,headers=headers,method='POST'))
    if (payload.get('ResponseMetadata') or {}).get('Error'): raise ValueError('query rejected')
    data=payload.get('Result') or {}
    if data.get('Currency')!='CNY': raise ValueError('non CNY balance')
    return balance_value(data.get('AvailableBalance'))


def aliyun_balance(credentials):
    access,secret=credentials['access_key_id'],credentials['access_key_secret']
    params={'Action':'QueryAccountBalance','Version':'2017-12-14','Format':'JSON','AccessKeyId':access,
            'SignatureMethod':'HMAC-SHA1','SignatureVersion':'1.0','SignatureNonce':uuid.uuid4().hex,
            'Timestamp':timezone.now().strftime('%Y-%m-%dT%H:%M:%SZ')}
    canonical=urlencode(sorted(params.items()),quote_via=quote,safe='~')
    sign='GET&%2F&'+quote(canonical,safe='~')
    params['Signature']=base64.b64encode(hmac.new((secret+'&').encode(),sign.encode(),hashlib.sha1).digest()).decode()
    payload=read_json(Request('https://business.aliyuncs.com/?'+urlencode(params)))
    if payload.get('Code')!='Success': raise ValueError('balance query rejected')
    data=payload.get('Data') or {}
    if data.get('Currency')!='CNY': raise ValueError('non CNY balance')
    return balance_value(data.get('AvailableAmount'))


def sync_account(account):
    now=timezone.now()
    account.attempted_at=now
    account.refresh_requested=False
    try:
        if account.vendor=='deepseek':
            key=os.getenv(account.key_env,'') if account.key_env else ''
            if not key: raise KeyError('configuration')
            data=read_json(Request('https://api.deepseek.com/user/balance',headers={'Authorization':'Bearer '+key}))
            row=next((r for r in data.get('balance_infos',[]) if r.get('currency')=='CNY'),None)
            value=balance_value(row.get('total_balance')) if row else None
        elif account.vendor in {'ali','volcano'}:
            credentials=decrypt_json(account.encrypted_credentials)
            if not credentials.get('access_key_id') or not credentials.get('access_key_secret'): raise KeyError('configuration')
            value=(aliyun_balance if account.vendor=='ali' else volcano_balance)(credentials)
        else:
            account.status='unconfigured'
            account.message='待接入官方余额查询；可前往服务商控制台查看。'
            account.save(update_fields=['attempted_at','refresh_requested','status','message'])
            return
        if value is None: raise ValueError('no CNY balance')
        account.balance_cny=value
        account.checked_at=now
        account.status='ok'
        account.message='官方账户余额'
    except KeyError:
        account.status='unconfigured'
        account.message='余额查询凭据待配置。'
    except Exception:
        account.status='error'
        account.message='余额同步失败；上次成功的余额已保留。'
    account.save(update_fields=['attempted_at','refresh_requested','balance_cny','checked_at','status','message'])


def sync_due_accounts():
    cutoff=timezone.now()-timedelta(hours=1)
    for account in BalanceAccount.objects.all():
        if account.refresh_requested or not account.attempted_at or account.attempted_at<cutoff:
            sync_account(account)
