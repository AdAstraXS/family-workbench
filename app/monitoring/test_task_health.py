from datetime import timedelta
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from family_core.models import Family, FamilyMember
from intelligence.program_models import ProgramEntry, ProgramRevision, ProgramSubscription, ProgramSummaryChunk
from intelligence.program_progress import program_progress
from investment_watch.models import NewsSource
from macro.models import MacroAlert, MacroIndicator, MacroImportRun, MacroObservation, MacroSourceMapping
from .task_health import task_health


class TaskHealthTests(TestCase):
    def setUp(self):
        self.family = Family.objects.create(name='回顾测试家庭')
        self.user = get_user_model().objects.create_user('health-member')
        self.member = FamilyMember.objects.create(family=self.family, user=self.user, display_name='本人', role='admin')
        self.other = FamilyMember.objects.create(family=self.family, display_name='其他成员')
        self.subscription = ProgramSubscription.objects.create(family=self.family, code='health-test')

    def entry(self, key, **kwargs):
        return ProgramEntry.objects.create(subscription=self.subscription, external_id=key, title=key, state='failed', **kwargs)

    def test_private_task_does_not_leak_to_admin(self):
        self.entry('家庭可见失败')
        self.entry('本人上传失败', private_owner=self.member)
        self.entry('其他成员私密失败', private_owner=self.other)
        other_family = Family.objects.create(name='另一个家庭')
        source = ProgramSubscription.objects.create(family=other_family, code='elsewhere')
        ProgramEntry.objects.create(subscription=source, external_id='foreign', title='跨家庭失败', state='failed')
        self.client.force_login(self.user)
        response = self.client.get(reverse('monitoring:tasks'))
        self.assertContains(response, '家庭可见失败')
        self.assertContains(response, '本人上传失败')
        self.assertNotContains(response, '其他成员私密失败')
        self.assertNotContains(response, '跨家庭失败')

    def test_uncertain_submission_is_not_presented_as_safe_retry(self):
        entry = self.entry('uncertain')
        entry.state = 'uncertain'
        progress = program_progress(entry)
        self.assertEqual(progress['stage'], '转写提交结果待核对')
        self.assertIn('避免重复提交', progress['next_step'])

    def test_summary_progress_reuses_saved_text_and_successful_parts(self):
        entry = self.entry('summary')
        revision = ProgramRevision.objects.create(entry=entry, content_hash='a'*64, origin='manual', text='原文')
        ProgramSummaryChunk.objects.create(revision=revision, number=1, status='success')
        ProgramSummaryChunk.objects.create(revision=revision, number=2, status='failed')
        entry.current_revision = revision
        progress = program_progress(entry)
        self.assertEqual(progress['stage'], 'AI 整理')
        self.assertIn('已保存 1 段 AI 整理结果', progress['completed'])
        self.assertEqual(ProgramSummaryChunk.objects.filter(status='failed').count(), 1)

    def test_successful_check_does_not_imply_current_macro_period(self):
        now = timezone.now()
        indicator = MacroIndicator.objects.create(country='US', code='health', name='统计期测试')
        mapping = MacroSourceMapping.objects.create(indicator=indicator, provider='test', group='test', definition={})
        observation = MacroObservation.objects.create(mapping=mapping, period_date='2025-01-01', value=1, last_seen_at=now)
        MacroImportRun.objects.create(group='test', status='success', finished_at=now)
        row = next(row for row in task_health(self.member)['sources'] if row['module'] == '宏观数据')
        self.assertEqual(str(row['period']), '2025-01-01')
        self.assertEqual(row['success'], now)
        self.assertEqual(row['status'], '按统计期核对')
        MacroAlert.objects.create(key='late:US:health', title='统计期未更新', message='本地验收', last_seen_at=now)
        row = next(row for row in task_health(self.member)['sources'] if row['module'] == '宏观数据')
        self.assertTrue(row['attention'])
        self.assertEqual(row['status'], '应更新统计期尚未取得')

    def test_news_check_and_success_are_separate(self):
        now = timezone.now()
        NewsSource.objects.create(family=self.family, key='test', name='新闻', enabled=True,
                                  last_checked_at=now, last_success_at=now-timedelta(days=2), last_error='获取失败')
        row = next(row for row in task_health(self.member)['sources'] if row['module'] == '财经资讯')
        self.assertEqual(row['checked'], now)
        self.assertEqual(row['success'], now-timedelta(days=2))
        self.assertEqual(row['status'], '获取失败')
