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
from .models import BalanceAccount, UsageRecord, HostSample, DownloadRecord, CollectorState, MODULES, VENDORS

CONSOLES={'deepseek':'https://platform.deepseek.com/usage','zhipu':'https://bigmodel.cn/console/overview',
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


def overview(family, period='month', vendor='all', unknown=False, page=1):
    now=timezone.now(); today=timezone.localdate(now)
    period=period if period in {'day','week','month'} else 'month'
    start_date=today if period=='day' else today-timedelta(days=6) if period=='week' else today.replace(day=1)
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
    accounts=[]
    by_vendor={r['vendor']:r['cost'] for r in records.values('vendor').annotate(cost=Sum('cost_cny'))}
    for account in BalanceAccount.objects.filter(family=family).order_by('vendor','pk'):
        account.stale=not account.checked_at or now-account.checked_at>timedelta(hours=2)
        account.low=account.balance_cny is not None and account.balance_cny<account.low_threshold
        account.period_cost=by_vendor.get(account.vendor)
        account.console=CONSOLES.get(account.vendor,'')
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
                     'tick':i in {0,count-1} or i%max(1,count//4)==0,'confirmed':has_price[i]})
    ranks=list(filtered.values('module').annotate(cost=Sum('cost_cny'),n=Count('id')).order_by('-cost'))
    for r in ranks:
        r['label']=MODULES.get(r['module'],r['module']);r['width']=float((r['cost'] or 0)/(agg['cost'] or 1)*100)
    samples=HostSample.objects.filter(sampled_at__gte=start,sampled_at__lt=end)
    traffic=samples.aggregate(up=Sum('upload_delta'),down=Sum('download_delta'),gaps=Count('id',filter=Q(gap=True)))
    traffic['total']=byte_label((traffic['up'] or 0)+(traffic['down'] or 0)) if traffic['up'] is not None else '—'
    traffic['upload']=byte_label(traffic['up']);traffic['download']=byte_label(traffic['down'])
    host=HostSample.objects.first(); subscriptions=[]
    if host:
        host.stale=now-host.sampled_at>timedelta(minutes=10)
        host.disk_free_label=byte_label(host.disk_free);host.disk_total_label=byte_label(host.disk_total)
        host.disk_percent=round((1-host.disk_free/host.disk_total)*100) if host.disk_total and host.disk_free is not None else None
        host.backup_stale=not host.backup_at or now-host.backup_at>timedelta(days=2)
        for sub in host.subscriptions:
            total=sub.get('total');up=sub.get('upload');down=sub.get('download')
            if total is None or up is None or down is None: continue
            used=up+down;expire=sub.get('expire')
            subscriptions.append({'name':sub['name'],'total':byte_label(total),'used':byte_label(used),
                'remaining':byte_label(max(0,total-used)),'percent':min(100,round(used/total*100,1)) if total else 0,
                'expires':datetime.fromtimestamp(expire,timezone.get_current_timezone()) if expire and expire<32503680000 else None})
    downloads=list(DownloadRecord.objects.filter(family=family).order_by('-created_at')[:5])
    for d in downloads: d.size=byte_label(d.file_bytes)
    logs=filtered.filter(status='unknown') if unknown else filtered
    return dict(period=period,period_label={'day':'今天','week':'近 7 天','month':'本月'}[period],vendor=vendor,
        vendors=VENDORS,agg=agg,accounts=accounts,bars=bars,ranks=ranks,traffic=traffic,host=host,
        subscriptions=subscriptions,downloads=downloads,logs=Paginator(logs,30).get_page(page),unknown=unknown,
        collector=CollectorState.objects.filter(key='main').first(),start_date=start_date,today=today,
        coverage_start=UsageRecord.objects.filter(family=family).order_by('started_at').values_list('started_at',flat=True).first(),
        network_start=HostSample.objects.order_by('sampled_at').values_list('sampled_at',flat=True).first())


@login_required
def index(request):
    ctx=overview(family_for(request),request.GET.get('period','month'),request.GET.get('vendor','all'),
                 request.GET.get('unknown')=='1',request.GET.get('page',1))
    ctx['can_configure']=can_configure(request)
    return render(request,'monitoring/index.html',ctx)


@login_required
@require_POST
def refresh(request):
    family=family_for(request)
    # Only queues safe, rate-limited reads; never performs slow vendor calls in a web request.
    BalanceAccount.objects.filter(family=family).filter(Q(attempted_at__isnull=True)|Q(attempted_at__lt=timezone.now()-timedelta(minutes=5))).update(refresh_requested=True)
    messages.success(request,'已请求刷新余额；下一次监控采集时更新，通常在 5 分钟内。')
    return redirect('monitoring:index')


class AccountForm(forms.Form):
    threshold=forms.DecimalField(label='低余额提醒（元）',min_value=0,max_digits=12,decimal_places=2)
    access_key_id=forms.CharField(label='只读 AccessKey ID',max_length=256,required=False,widget=forms.PasswordInput(render_value=False))
    access_key_secret=forms.CharField(label='只读 AccessKey Secret',max_length=256,required=False,widget=forms.PasswordInput(render_value=False))

    def clean(self):
        data=super().clean()
        if bool(data.get('access_key_id'))!=bool(data.get('access_key_secret')):
            raise forms.ValidationError('请同时填写 AccessKey ID 和 Secret，或同时留空保留原配置。')
        return data


@login_required
@sensitive_post_parameters('access_key_id','access_key_secret')
def settings_view(request):
    if not can_configure(request): raise PermissionDenied
    family=family_for(request)
    accounts=BalanceAccount.objects.filter(family=family).order_by('vendor')
    form=None;selected=None
    if request.method=='POST':
        selected=get_object_or_404(accounts,pk=request.POST.get('account'))
        form=AccountForm(request.POST)
        if form.is_valid():
            selected.low_threshold=form.cleaned_data['threshold']
            if form.cleaned_data['access_key_id']:
                if selected.vendor not in {'ali','volcano'}:
                    form.add_error(None,'此凭据入口适用于阿里云与火山账户。')
                else:
                    selected.encrypted_credentials=encrypt_json({'access_key_id':form.cleaned_data['access_key_id'],
                        'access_key_secret':form.cleaned_data['access_key_secret']})
            if not form.errors:
                selected.refresh_requested=True
                selected.save(update_fields=['low_threshold','encrypted_credentials','refresh_requested'])
                messages.success(request,'账户设置已保存，敏感凭据不会回显。')
                return redirect('monitoring:settings')
    from ai_analysis.models import AiProvider
    from .pricing import rate_snapshot
    providers=[{'provider':p,'rates':rate_snapshot(p)} for p in AiProvider.objects.filter(is_active=True)]
    return render(request,'monitoring/settings.html',{'accounts':accounts,'form':form,'selected':selected,'providers':providers})
