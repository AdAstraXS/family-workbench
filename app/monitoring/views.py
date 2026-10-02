from datetime import datetime, time, timedelta
from decimal import Decimal
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Sum, Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.views.decorators.debug import sensitive_post_parameters
from django import forms
from family_core.household import get_household_family
from knowledge.crypto import encrypt_json
from .models import BalanceAccount, ModelAllowance, UsageRecord, HostSample, DownloadRecord, CollectorState, MODULES, VENDORS
from .cadence import COLLECTION_MINUTES, STALE_AFTER

CONSOLES={'deepseek':'https://platform.deepseek.com/usage','zhipu':'https://bigmodel.cn/finance-center/finance/overview',
          'volcano':'https://console.volcengine.com/finance/overview/','ali':'https://usercenter2.aliyun.com/'}


def family_for(request):
    member=getattr(request,'family_member',None)
    if member: return member.family
    if request.user.is_superuser:
        family=get_household_family()
        if family: return family
    raise PermissionDenied


def can_configure(request):
    member=getattr(request,'family_member',None)
    return request.user.is_superuser or bool(member and member.role=='admin')


def byte_label(n):
    if n is None: return '—'
    for factor,label in ((10**12,'TB'),(10**9,'GB'),(10**6,'MB'),(10**3,'KB')):
        if n>=factor: return f'{n/factor:.2f} {label}'
    return f'{n} B'


def quota_label(n):
    """Proxy subscriptions report bytes; providers commonly name GiB as GB."""
    return f'{n / 1024**3:.2f} GiB' if n is not None else '—'


def configured_models():
    from ai_analysis.models import AiProvider
    from .pricing import vendor_for
    names={v:set() for v,_ in VENDORS}
    for provider in AiProvider.objects.filter(is_active=True):
        names.setdefault(vendor_for(provider),set()).add(provider.model_name)
    names['ali'].add('fun-asr-2025-11-07')
    return names


def overview(family, period='30days', vendor='all', unknown=False, page=1):
    now=timezone.now(); today=timezone.localdate(now)
    period=period if period in {'day','week','30days'} else '30days'
    start_date=today if period=='day' else today-timedelta(days=6) if period=='week' else today-timedelta(days=29)
    start=timezone.make_aware(datetime.combine(start_date,time.min))
    end=timezone.make_aware(datetime.combine(today+timedelta(days=1),time.min))
    records=UsageRecord.objects.filter(family=family,started_at__gte=start,started_at__lt=end)
    if vendor not in dict(VENDORS): vendor='all'
    filtered=records if vendor=='all' else records.filter(vendor=vendor)
    agg=filtered.aggregate(cost=Sum('cost_cny'),tokens=Sum('total_tokens'),audio=Sum('audio_seconds'),n=Count('id'),
        unknown=Count('id',filter=Q(status='unknown')),pending=Count('id',filter=Q(status='pending')),
        unpriced=Count('id',filter=Q(status='confirmed',cost_cny__isnull=True)))
    agg['audio_minutes']=(agg['audio'] or Decimal(0))/60
    agg['cost_label']=f"¥{agg['cost']:.2f}" if agg['cost'] is not None else ('—' if agg['n'] else '¥0.00')
    agg['tokens_label']=f"{agg['tokens']/1000000:.3f}" if agg['tokens'] is not None else ('—' if agg['unknown'] or agg['pending'] else '0.000')
    model_totals=list(records.values('vendor','model_name').annotate(
        calls=Count('id'),tokens=Sum('total_tokens'),audio=Sum('audio_seconds'),cost=Sum('cost_cny')).order_by('model_name'))
    configured=configured_models()
    accounts=[]
    allowances={(a.account_id,a.model_name):a for a in ModelAllowance.objects.filter(account__family=family)}
    by_vendor={r['vendor']:r['cost'] for r in records.values('vendor').annotate(cost=Sum('cost_cny'))}
    for account in BalanceAccount.objects.filter(family=family).order_by('vendor','pk'):
        max_age=timedelta(days=1) if account.status=='manual' else timedelta(hours=2)
        account.stale=not account.checked_at or now-account.checked_at>max_age
        account.low=account.balance_cny is not None and account.balance_cny<account.low_threshold
        account.period_cost=by_vendor.get(account.vendor)
        account.console=CONSOLES.get(account.vendor,'')
        rows={row['model_name']:row for row in model_totals if row['vendor']==account.vendor}
        account.models=[]
        for name in sorted(configured.get(account.vendor,set()) | rows.keys()):
            if not name: continue
            row=rows.get(name,{})
            audio=row.get('audio') or Decimal(0)
            account.models.append({'name':name,'calls':row.get('calls',0),'tokens':row.get('tokens'),
                'audio_minutes':audio/60 if audio else None,'cost':row.get('cost'),
                'allowance':allowances.get((account.pk,name))})
        accounts.append(account)
    count=24 if period=='day' else (today-start_date).days+1
    amounts=[Decimal(0) for _ in range(count)]
    has_price=[False]*count
    for row in filtered.filter(cost_cny__isnull=False).values('started_at','cost_cny'):
        dt=timezone.localtime(row['started_at']);i=dt.hour if period=='day' else (dt.date()-start_date).days
        amounts[i]+=row['cost_cny'];has_price[i]=True
    maximum=max(amounts,default=Decimal(0)) or Decimal(1)
    bars=[]
    for i,value in enumerate(amounts):
        label=f'{i:02}:00' if period=='day' else (start_date+timedelta(days=i)).strftime('%m/%d')
        bars.append({'label':label,'amount':str(value.quantize(Decimal('.0001'))),'height':float(value/maximum*100),
                     'amount_label':f'¥{value:.2f}' if value>=Decimal('.01') else f'¥{value:.4f}',
                     'tick':i in {0,count-1} or i%max(1,count//4)==0,'confirmed':has_price[i]})
    ranks=list(filtered.values('module').annotate(cost=Sum('cost_cny'),n=Count('id')).order_by('-cost'))
    for r in ranks:
        r['label']=MODULES.get(r['module'],r['module']);r['width']=float((r['cost'] or 0)/(agg['cost'] or 1)*100)
    samples=HostSample.objects.filter(sampled_at__gte=start,sampled_at__lt=end)
    traffic=samples.aggregate(up=Sum('upload_delta'),down=Sum('download_delta'),gaps=Count('id',filter=Q(gap=True)))
    traffic['total']=byte_label((traffic['up'] or 0)+(traffic['down'] or 0)) if traffic['up'] is not None else '—'
    traffic['upload']=byte_label(traffic['up']);traffic['download']=byte_label(traffic['down'])
    traffic_amounts=[0]*count
    traffic_sampled=[False]*count
    for row in samples.values('sampled_at','upload_delta','download_delta'):
        if row['upload_delta'] is None or row['download_delta'] is None:
            continue
        dt=timezone.localtime(row['sampled_at'])
        i=dt.hour if period=='day' else (dt.date()-start_date).days
        traffic_amounts[i]+=row['upload_delta']+row['download_delta']
        traffic_sampled[i]=True
    traffic_max=max(traffic_amounts,default=0)
    factor,unit=next(((factor,unit) for factor,unit in (
        (1024**3,'GiB'),(1024**2,'MiB'),(1024,'KiB')) if traffic_max>=factor),(1,'B'))
    traffic['unit']=unit
    traffic['chart_total']=f'{sum(traffic_amounts)/factor:.2f} {unit}' if any(traffic_sampled) else '—'
    traffic['bars']=[{
        'label':bars[i]['label'],'tick':bars[i]['tick'],'confirmed':traffic_sampled[i],
        'bytes':value,'amount':f'{value/factor:.2f}',
        'height':value/(traffic_max or 1)*100,
    } for i,value in enumerate(traffic_amounts)]
    host=HostSample.objects.first(); subscriptions=[]
    if host:
        host.stale=now-host.sampled_at>STALE_AFTER
        host.disk_free_label=byte_label(host.disk_free);host.disk_total_label=byte_label(host.disk_total)
        host.disk_percent=round((1-host.disk_free/host.disk_total)*100) if host.disk_total and host.disk_free is not None else None
        host.backup_stale=not host.backup_at or now-host.backup_at>timedelta(days=2)
        for sub in host.subscriptions:
            total=sub.get('total');up=sub.get('upload');down=sub.get('download')
            if total is None or up is None or down is None: continue
            used=up+down;expire=sub.get('expire')
            subscriptions.append({'name':sub['name'],'total':quota_label(total),'used':quota_label(used),
                'remaining':quota_label(max(0,total-used)),'percent':min(100,round(used/total*100,1)) if total else 0,
                'expires':datetime.fromtimestamp(expire,timezone.get_current_timezone()) if expire and expire<32503680000 else None})
    downloads=list(DownloadRecord.objects.filter(family=family).order_by('-created_at')[:5])
    if downloads:
        from intelligence.program_models import ProgramEntry
        from intelligence.program_sources import CATALOGUE
        entries={entry.pk:entry for entry in ProgramEntry.objects.filter(
            pk__in=[d.entry_id for d in downloads],subscription__family=family).select_related('subscription')}
        for d in downloads:
            d.size=byte_label(d.file_bytes)
            entry=entries.get(d.entry_id)
            if entry:
                sub=entry.subscription
                d.subscription_name=sub.custom_name or CATALOGUE.get(sub.code,{}).get('name') or sub.code
                d.entry_title=entry.title
            else:
                d.subscription_name=d.source
                d.entry_title=f'节目 #{d.entry_id}'
    logs=filtered.filter(status='unknown') if unknown else filtered
    return dict(period=period,period_label={'day':'今天','week':'近 7 天','30days':'近 30 天'}[period],vendor=vendor,
        vendors=VENDORS,selected_vendor_label=dict(VENDORS).get(vendor,''),agg=agg,accounts=accounts,bars=bars,ranks=ranks,traffic=traffic,host=host,
        subscriptions=subscriptions,downloads=downloads,logs=Paginator(logs,30).get_page(page),unknown=unknown,
        collector=CollectorState.objects.filter(key='main').first(),collection_minutes=COLLECTION_MINUTES,start_date=start_date,today=today,
        coverage_start=UsageRecord.objects.filter(family=family).order_by('started_at').values_list('started_at',flat=True).first(),
        network_start=HostSample.objects.order_by('sampled_at').values_list('sampled_at',flat=True).first())


@login_required
def index(request):
    ctx=overview(family_for(request),request.GET.get('period','30days'),request.GET.get('vendor','all'),
                 request.GET.get('unknown')=='1',request.GET.get('page',1))
    ctx['can_configure']=can_configure(request)
    return render(request,'monitoring/index.html',ctx)


@login_required
@require_POST
def refresh(request):
    family=family_for(request)
    # Only queues safe, rate-limited reads; never performs slow vendor calls in a web request.
    BalanceAccount.objects.filter(family=family).filter(Q(attempted_at__isnull=True)|Q(attempted_at__lt=timezone.now()-timedelta(minutes=5))).update(refresh_requested=True)
    messages.success(request,f'已请求刷新余额；下一次监控采集时更新，通常在 {COLLECTION_MINUTES} 分钟内。')
    return redirect('monitoring:index')


class AccountForm(forms.Form):
    threshold=forms.DecimalField(label='低余额提醒（元）',min_value=0,max_digits=12,decimal_places=2)
    manual_balance=forms.DecimalField(label='官网当前可用余额（元）',max_digits=18,decimal_places=2,required=False)
    access_key_id=forms.CharField(label='只读 AccessKey ID',max_length=256,required=False,widget=forms.PasswordInput(render_value=False))
    access_key_secret=forms.CharField(label='只读 AccessKey Secret',max_length=256,required=False,widget=forms.PasswordInput(render_value=False))

    def clean(self):
        data=super().clean()
        if bool(data.get('access_key_id'))!=bool(data.get('access_key_secret')):
            raise forms.ValidationError('请同时填写 AccessKey ID 和 Secret，或同时留空保留原配置。')
        return data


class AllowanceForm(forms.Form):
    model_name=forms.CharField(max_length=150)
    remaining=forms.DecimalField(label='官网剩余额度',min_value=0,max_digits=18,decimal_places=2)
    unit=forms.ChoiceField(choices=ModelAllowance.UNITS)


@login_required
@sensitive_post_parameters('access_key_id','access_key_secret')
def settings_view(request):
    if not can_configure(request): raise PermissionDenied
    family=family_for(request)
    accounts=BalanceAccount.objects.filter(family=family).order_by('vendor')
    form=None;selected=None;allowance_form=None
    if request.method=='POST':
        selected=get_object_or_404(accounts,pk=request.POST.get('account'))
        if request.POST.get('mode')=='allowance':
            allowance_form=AllowanceForm(request.POST)
            if allowance_form.is_valid():
                name=allowance_form.cleaned_data['model_name']
                allowed=configured_models().get(selected.vendor,set()) | set(UsageRecord.objects.filter(
                    family=family,vendor=selected.vendor).values_list('model_name',flat=True))
                if name not in allowed:
                    allowance_form.add_error('model_name','只能记录此账户已配置或已调用模型的额度。')
                else:
                    ModelAllowance.objects.update_or_create(account=selected,model_name=name,defaults={
                        'remaining':allowance_form.cleaned_data['remaining'],'unit':allowance_form.cleaned_data['unit'],
                        'checked_at':timezone.now()})
                    messages.success(request,'官网模型额度已记录。')
                    return redirect('monitoring:settings')
        else:
            form=AccountForm(request.POST)
        if form is not None and form.is_valid():
            selected.low_threshold=form.cleaned_data['threshold']
            manual=form.cleaned_data['manual_balance']
            if manual is not None:
                if selected.vendor!='zhipu': form.add_error('manual_balance','仅智谱支持手动记录官网余额。')
                else:
                    selected.balance_cny=manual
                    selected.checked_at=timezone.now()
                    selected.status='manual'
                    selected.message='官网余额手动记录；请定期核对。'
            if form.cleaned_data['access_key_id']:
                if selected.vendor not in {'ali','volcano'}:
                    form.add_error(None,'此凭据入口适用于阿里云与火山账户。')
                else:
                    selected.encrypted_credentials=encrypt_json({'access_key_id':form.cleaned_data['access_key_id'],
                        'access_key_secret':form.cleaned_data['access_key_secret']})
            if not form.errors:
                selected.refresh_requested=(selected.vendor!='zhipu' and
                    (selected.refresh_requested or bool(form.cleaned_data['access_key_id'])))
                selected.save(update_fields=['low_threshold','encrypted_credentials','balance_cny','checked_at','status','message','refresh_requested'])
                messages.success(request,'账户设置已保存，敏感凭据不会回显。')
                return redirect('monitoring:settings')
    from ai_analysis.models import AiProvider
    from .pricing import rate_snapshot
    providers=[{'provider':p,'rates':rate_snapshot(p)} for p in AiProvider.objects.filter(is_active=True)]
    configured=configured_models()
    used=UsageRecord.objects.filter(family=family).values_list('vendor','model_name').distinct()
    for vendor,name in used: configured.setdefault(vendor,set()).add(name)
    for account in accounts:
        account.allowance_by_name={row.model_name:row for row in account.model_allowances.all()}
        account.allowance_models=[{'name':name,'record':account.allowance_by_name.get(name)}
            for name in sorted(configured.get(account.vendor,set())) if name]
    return render(request,'monitoring/settings.html',{'accounts':accounts,'form':form,'selected':selected,
        'allowance_form':allowance_form,'providers':providers})
