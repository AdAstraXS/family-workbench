import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from ai_analysis.models import AiProvider
from family_core.models import Family, FamilyMember
from .models import UsageRecord, BalanceAccount, ModelAllowance, HostSample, DownloadRecord
from .metering import tracked_call, capture_asr
from .balances import ensure_accounts, sync_account
from .collection import ingest_host
from .views import overview


class MonitorTests(TestCase):
    def setUp(self):
        self.family=Family.objects.create(name='测试家庭')
        self.user=get_user_model().objects.create_user('monitor',password='test-only')
        self.member=FamilyMember.objects.create(family=self.family,user=self.user,display_name='成员',role='member')
        self.provider=AiProvider.objects.create(name='DeepSeek',provider_type='openai_compatible',base_url='https://api.deepseek.com',model_name='deepseek-flash',extra_data={'api_key_env_var':'TEST_MODEL_KEY'})

    def call(self,reply):
        return tracked_call(lambda:reply,provider=self.provider,module='knowledge',family_id=self.family.pk)

    def test_cache_is_not_double_counted(self):
        self.call({'usage':{'prompt_tokens':1000,'completion_tokens':200,'prompt_cache_hit_tokens':600}})
        row=UsageRecord.objects.get()
        self.assertEqual(row.total_tokens,1200)
        self.assertEqual(row.cost_cny,Decimal('.002424'))
        self.assertEqual(row.status,'confirmed')

    def test_invalid_business_output_still_counts_usage(self):
        self.call(json.dumps({'choices':[], 'usage':{'prompt_tokens':100,'completion_tokens':30}}).encode())
        self.assertEqual(UsageRecord.objects.get().total_tokens,130)

    def test_missing_usage_is_unknown_not_zero(self):
        self.call({'choices':[]})
        row=UsageRecord.objects.get()
        self.assertEqual(row.status,'unknown');self.assertIsNone(row.total_tokens);self.assertIsNone(row.cost_cny)

    def test_zero_usage_is_valid(self):
        self.call({'usage':{'prompt_tokens':0,'completion_tokens':0}})
        self.assertEqual(UsageRecord.objects.get().cost_cny,0)

    def test_boolean_negative_usage_rejected(self):
        self.call({'usage':{'prompt_tokens':True,'completion_tokens':-1,'total_tokens':False}})
        self.assertEqual(UsageRecord.objects.get().status,'unknown')

    def test_total_only_has_no_guessed_cost(self):
        self.call({'usage':{'total_tokens':1000}})
        self.assertEqual(UsageRecord.objects.get().total_tokens,1000)
        self.assertIsNone(UsageRecord.objects.get().cost_cny)

    def test_retries_are_separate_attempts(self):
        for _ in range(2):self.call({'usage':{'total_tokens':10}})
        self.assertEqual(UsageRecord.objects.count(),2)

    def test_failure_preserves_attempt(self):
        def broken():raise TimeoutError('secret response must not be stored')
        with self.assertRaises(TimeoutError):
            tracked_call(broken,provider=self.provider,module='knowledge',family_id=self.family.pk)
        row=UsageRecord.objects.get();self.assertEqual(row.status,'unknown');self.assertEqual(row.outcome,'request_error')
        self.assertNotIn('secret',str(row.__dict__))

    def test_old_rate_snapshot_is_immutable(self):
        self.call({'usage':{'prompt_tokens':1000,'completion_tokens':200}})
        old=UsageRecord.objects.get().cost_cny
        self.provider.extra_data={'monitoring_cny_rates':{'input':'10','output':'20'}};self.provider.save()
        self.assertEqual(UsageRecord.objects.get().cost_cny,old)
        self.call({'usage':{'prompt_tokens':1000,'completion_tokens':200}})
        self.assertEqual(UsageRecord.objects.first().cost_cny,Decimal('.014'))

    def test_telemetry_failure_does_not_repeat_or_block_model(self):
        with patch('monitoring.metering.UsageRecord.objects.create',side_effect=RuntimeError('db')):
            self.assertEqual(self.call({'result':'ok'}),{'result':'ok'})

    def test_asr_polling_idempotent(self):
        tracked_call(lambda:{'output':{'task_id':'task1','task_status':'PENDING'}},provider=None,module='programs',family_id=self.family.pk,audio=True)
        response={'output':{'task_id':'task1','task_status':'SUCCEEDED'},'usage':{'duration':90}}
        capture_asr('task1',self.family.pk,response);capture_asr('task1',self.family.pk,response)
        row=UsageRecord.objects.get();self.assertEqual(row.audio_seconds,90);self.assertEqual(row.cost_cny,Decimal('.0198'))

    def test_existing_asr_not_silently_backfilled(self):
        capture_asr('old',self.family.pk,{'output':{'task_status':'SUCCEEDED'},'usage':{'duration':90}})
        self.assertEqual(UsageRecord.objects.count(),0)

    def test_models_sharing_credential_have_one_account(self):
        AiProvider.objects.create(name='another',base_url=self.provider.base_url,model_name='deepseek-v4-pro',extra_data=self.provider.extra_data)
        ensure_accounts(self.family);ensure_accounts(self.family)
        self.assertEqual(BalanceAccount.objects.filter(vendor='deepseek',account_key='TEST_MODEL_KEY').count(),1)

    def test_balance_failure_preserves_last_success(self):
        a=BalanceAccount.objects.create(family=self.family,vendor='deepseek',label='D',key_env='TEST_MODEL_KEY',balance_cny=10,checked_at=timezone.now())
        with patch.dict('os.environ',{'TEST_MODEL_KEY':'fake'}),patch('monitoring.balances.read_json',side_effect=TimeoutError):sync_account(a)
        a.refresh_from_db();self.assertEqual(a.balance_cny,10);self.assertEqual(a.status,'error')

    def test_official_negative_balance_is_preserved(self):
        a=BalanceAccount.objects.create(family=self.family,vendor='deepseek',label='D',key_env='TEST_MODEL_KEY')
        with patch.dict('os.environ',{'TEST_MODEL_KEY':'fake'}),patch('monitoring.balances.read_json',return_value={'balance_infos':[{'currency':'CNY','total_balance':'-2.5'}]}):sync_account(a)
        self.assertEqual(a.balance_cny,Decimal('-2.5'))

    def test_cloud_balance_queries_validate_currency_and_signed_transport(self):
        from .balances import volcano_balance, aliyun_balance
        creds={'access_key_id':'test-ak','access_key_secret':'test-sk'}
        with patch('monitoring.balances.read_json',return_value={'Result':{'Currency':'CNY','AvailableBalance':'12.50'}}) as read:
            self.assertEqual(volcano_balance(creds),Decimal('12.50'))
            req=read.call_args.args[0]
            self.assertEqual(req.get_method(),'POST')
            self.assertTrue(req.full_url.startswith('https://open.volcengineapi.com/'))
            self.assertIn('SignedHeaders=',req.get_header('Authorization'))
            self.assertNotIn('test-sk',req.full_url+str(req.headers))
        with patch('monitoring.balances.read_json',return_value={'Code':'200','Success':True,'Data':{'Currency':'CNY','AvailableAmount':'1,234.50'}}):
            self.assertEqual(aliyun_balance(creds),Decimal('1234.50'))
        with patch('monitoring.balances.read_json',return_value={'Code':'200','Success':False,'Data':{'Currency':'CNY','AvailableAmount':'1,234.50'}}):
            with self.assertRaises(ValueError):aliyun_balance(creds)
        with patch('monitoring.balances.read_json',return_value={'Result':{'Currency':'USD','AvailableBalance':'12.50'}}):
            with self.assertRaises(ValueError):volcano_balance(creds)

    def test_terabyte_disk_and_quota_are_not_dropped(self):
        row=ingest_host({'sampled_at':timezone.now().isoformat(),'disk_total':4000000000000,'disk_free':2300000000000,
                        'subscriptions':[{'name':'test','total':2000000000000,'upload':0,'download':20}]})
        self.assertEqual(row.disk_total,4000000000000)
        self.assertEqual(row.subscriptions[0]['total'],2000000000000)

    def test_unknown_tokens_are_not_rendered_as_zero(self):
        self.call({})
        self.assertEqual(overview(self.family)['agg']['tokens_label'],'—')

    @override_settings(DEBUG=True)
    def test_only_admin_can_save_encrypted_balance_credentials(self):
        from knowledge.crypto import decrypt_json
        a=BalanceAccount.objects.create(family=self.family,vendor='volcano',label='V')
        self.client.force_login(self.user)
        data={'account':a.pk,'threshold':'10','access_key_id':'test-ak','access_key_secret':'test-secret'}
        self.assertEqual(self.client.post(reverse('monitoring:settings'),data).status_code,403)
        self.member.role='admin';self.member.save()
        self.assertEqual(self.client.post(reverse('monitoring:settings'),data).status_code,302)
        a.refresh_from_db()
        self.assertNotIn('test-secret',a.encrypted_credentials)
        self.assertEqual(decrypt_json(a.encrypted_credentials)['access_key_secret'],'test-secret')
        self.assertNotContains(self.client.get(reverse('monitoring:settings')),'test-secret')

    def test_zhipu_tier_boundary_and_cache(self):
        self.provider.base_url='https://open.bigmodel.cn/api/paas/v4';self.provider.model_name='glm-5v-turbo'
        for n in [32767,32768]:self.call({'usage':{'prompt_tokens':n,'completion_tokens':1000,'prompt_tokens_details':{'cached_tokens':1000}}})
        self.assertEqual(UsageRecord.objects.first().cost_cny,Decimal('.250176'))

    def test_non_cny_balance_not_mislabelled(self):
        a=BalanceAccount.objects.create(family=self.family,vendor='deepseek',label='D',key_env='TEST_MODEL_KEY')
        with patch.dict('os.environ',{'TEST_MODEL_KEY':'fake'}),patch('monitoring.balances.read_json',return_value={'balance_infos':[{'currency':'USD','total_balance':'30'}]}):sync_account(a)
        self.assertIsNone(a.balance_cny)

    def test_host_baseline_delta_reset_and_duplicate(self):
        now=timezone.now()-timedelta(minutes=3)
        def payload(i,up,down,epoch='a'):return {'sampled_at':(now+timedelta(minutes=i)).isoformat(),'proxy_ok':True,'upload_total':up,'download_total':down,'session_id':epoch}
        first=ingest_host(payload(0,100,500));self.assertIsNone(first.download_delta)
        next_=ingest_host(payload(1,130,800));self.assertEqual(next_.download_delta,300)
        duplicate=ingest_host(payload(1,130,800));self.assertEqual(next_.pk,duplicate.pk)
        reset=ingest_host(payload(2,10,20,'b'));self.assertTrue(reset.gap);self.assertIsNone(reset.download_delta)
        self.assertEqual(HostSample.objects.count(),3)

    def test_host_out_of_order_rejected(self):
        now=timezone.now();ingest_host({'sampled_at':now.isoformat()})
        with self.assertRaises(ValueError):ingest_host({'sampled_at':(now-timedelta(seconds=1)).isoformat()})

    def test_half_hour_collection_and_missed_runs(self):
        now=timezone.now()
        HostSample.objects.create(sampled_at=now-timedelta(minutes=30),proxy_ok=True,
            upload_total=100,download_total=500,session_id='same')
        self.assertFalse(overview(self.family)['host'].stale)
        row=ingest_host({'sampled_at':now.isoformat(),'proxy_ok':True,
            'upload_total':150,'download_total':900,'session_id':'same'})
        self.assertFalse(row.gap)
        self.assertEqual(row.download_delta,400)
        with patch('monitoring.views.timezone.now',return_value=now+timedelta(minutes=66)):
            self.assertTrue(overview(self.family)['host'].stale)
        with patch('monitoring.collection.timezone.now',return_value=now+timedelta(minutes=66)):
            late=ingest_host({'sampled_at':(now+timedelta(minutes=66)).isoformat(),
                'proxy_ok':True,'upload_total':200,'download_total':1000,'session_id':'same'})
        self.assertTrue(late.gap)
        self.assertEqual(late.download_delta,100)

    def test_get_is_read_only_and_all_members_can_view(self):
        self.client.force_login(self.user)
        before=BalanceAccount.objects.count()
        response=self.client.get(reverse('monitoring:index'))
        self.assertEqual(response.status_code,200);self.assertEqual(BalanceAccount.objects.count(),before)
        self.member.role='viewer';self.member.save()
        self.assertEqual(self.client.get(reverse('monitoring:index')).status_code,200)
        self.assertEqual(self.client.get(reverse('monitoring:settings')).status_code,403)

    def test_family_isolation(self):
        self.call({'usage':{'total_tokens':12345}})
        other=Family.objects.create(name='other')
        self.assertEqual(overview(other)['agg']['n'],0)

    def test_private_content_and_credentials_absent(self):
        self.call({'usage':{'total_tokens':100},'choices':[{'message':{'content':'private-test-content'}}]})
        BalanceAccount.objects.create(family=self.family,vendor='ali',label='Ali',encrypted_credentials='secret-ciphertext')
        self.client.force_login(self.user)
        response=self.client.get(reverse('monitoring:index'))
        self.assertNotContains(response,'private-test-content');self.assertNotContains(response,'secret-ciphertext')

    def test_refresh_queues_only_and_csrf_required(self):
        ensure_accounts(self.family);self.client.force_login(self.user)
        with patch('monitoring.balances.read_json',side_effect=AssertionError('must not call from web')):
            self.assertEqual(self.client.post(reverse('monitoring:refresh')).status_code,302)
        self.assertTrue(BalanceAccount.objects.filter(refresh_requested=True).exists())
        from django.test import Client
        secure=Client(enforce_csrf_checks=True);secure.force_login(self.user)
        self.assertEqual(secure.post(reverse('monitoring:refresh')).status_code,403)

    def test_period_chart_and_ranking_reconcile(self):
        self.call({'usage':{'prompt_tokens':1000,'completion_tokens':200}})
        ctx=overview(self.family)
        self.assertEqual(sum(Decimal(b['amount']) for b in ctx['bars']),ctx['agg']['cost'].quantize(Decimal('.0001')))
        self.assertEqual(sum(r['cost'] for r in ctx['ranks']),ctx['agg']['cost'])

    def test_account_card_filters_chart_and_shows_model_usage(self):
        BalanceAccount.objects.create(family=self.family,vendor='deepseek',label='DeepSeek')
        BalanceAccount.objects.create(family=self.family,vendor='zhipu',label='智谱')
        self.call({'usage':{'prompt_tokens':1000,'completion_tokens':200}})
        self.client.force_login(self.user)
        page=self.client.get(reverse('monitoring:index'),{'vendor':'deepseek','period':'week'})
        self.assertContains(page,'?period=week&amp;vendor=deepseek#cost-trend')
        self.assertContains(page,'AI 费用趋势 · DeepSeek')
        self.assertContains(page,'deepseek-flash')
        self.assertContains(page,'1200 Token')
        self.assertContains(page,'mon-bar-value')
        self.assertEqual(page.context['agg']['n'],1)
        zhipu=self.client.get(reverse('monitoring:index'),{'vendor':'zhipu'})
        self.assertEqual(zhipu.context['agg']['n'],0)
        self.assertContains(zhipu,'0 次 · 暂无调用')

    def test_manual_zhipu_balance_survives_collection(self):
        account=BalanceAccount.objects.create(family=self.family,vendor='zhipu',label='智谱')
        self.member.role='admin';self.member.save()
        self.client.force_login(self.user)
        response=self.client.post(reverse('monitoring:settings'),{'account':account.pk,'threshold':'20','manual_balance':'60.29'})
        self.assertEqual(response.status_code,302)
        account.refresh_from_db()
        self.assertEqual(account.balance_cny,Decimal('60.29'))
        self.assertEqual(account.status,'manual')
        self.assertFalse(account.refresh_requested)
        original_checked=account.checked_at
        sync_account(account)
        account.refresh_from_db()
        self.assertEqual(account.balance_cny,Decimal('60.29'))
        self.assertEqual(account.checked_at,original_checked)
        self.assertContains(self.client.get(reverse('monitoring:index')),'官网余额手动记录')

    def test_quota_units_and_download_subscription_title(self):
        from intelligence.program_models import ProgramSubscription, ProgramEntry
        host=HostSample.objects.create(sampled_at=timezone.now(),subscriptions=[{
            'name':'套餐','total':260*1024**3,'upload':2*1024**3,'download':3*1024**3}])
        sub=ProgramSubscription.objects.create(family=self.family,code='rhino',kind='youtube')
        entry=ProgramEntry.objects.create(subscription=sub,external_id='example',title='今日节目',url='https://youtube.com/watch?v=example')
        DownloadRecord.objects.create(family=self.family,entry_id=entry.pk,source='youtube',file_bytes=1024,media_type='audio/mp4')
        ctx=overview(self.family)
        self.assertEqual(ctx['subscriptions'][0]['total'],'260.00 GiB')
        self.assertEqual(ctx['subscriptions'][0]['remaining'],'255.00 GiB')
        self.assertEqual(ctx['downloads'][0].subscription_name,'视野环球财经')
        self.assertEqual(ctx['downloads'][0].entry_title,'今日节目')

    def test_allowance_requires_admin_and_known_model_and_retains_verified_value(self):
        account=BalanceAccount.objects.create(family=self.family,vendor='deepseek',label='DeepSeek')
        self.client.force_login(self.user)
        data={'mode':'allowance','account':account.pk,'model_name':'deepseek-flash','remaining':'123.5','unit':'token'}
        self.assertEqual(self.client.post(reverse('monitoring:settings'),data).status_code,403)
        self.member.role='admin';self.member.save()
        bad={**data,'model_name':'unconfigured-model'}
        self.assertEqual(self.client.post(reverse('monitoring:settings'),bad).status_code,200)
        self.assertFalse(ModelAllowance.objects.exists())
        self.assertEqual(self.client.post(reverse('monitoring:settings'),data).status_code,302)
        allowance=ModelAllowance.objects.get(account=account,model_name='deepseek-flash')
        self.assertEqual(allowance.remaining,Decimal('123.5'))
        self.call({'usage':{'prompt_tokens':100,'completion_tokens':10}})
        self.assertEqual(overview(self.family)['accounts'][0].models[0]['allowance'].remaining,Decimal('123.5'))
        self.assertContains(self.client.get(reverse('monitoring:index')),'赠送余额 123.50 Token')
