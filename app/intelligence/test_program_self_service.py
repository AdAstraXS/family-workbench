import io
import json
import tempfile
import wave
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from family_core.models import Family, FamilyMember
from knowledge.models import KnowledgeDocument, KnowledgeVisibility
from .program_custom_sources import matches_filters, video_items
from .program_models import ProgramEntry, ProgramSubscription
from .program_sources import collect_subscription


@override_settings(KNOWLEDGE_TOKEN_ENCRYPTION_KEY=Fernet.generate_key().decode())
class SelfServiceProgramTests(TestCase):
    def setUp(self):
        self.family = Family.objects.create(name='自助订阅家庭')
        self.owner = FamilyMember.objects.create(family=self.family,
            user=get_user_model().objects.create_user(username='source-owner'),
            display_name='本人', role='admin')
        self.other = FamilyMember.objects.create(family=self.family,
            user=get_user_model().objects.create_user(username='source-other'),
            display_name='其他成员', role='member')
        self.client.force_login(self.owner.user)

    @patch('intelligence.program_views.inspect_new_source')
    def test_preview_save_edit_and_pause_source(self, inspect):
        inspect.return_value = {'name': '宏观访谈', 'source_url': 'https://example.com/feed.xml',
            'feed_url': 'https://example.com/feed.xml', 'channel_id': '', 'playlist_id': '',
            'items': [{'title': '大摩宏观策略谈', 'published_at': timezone.now(), 'duration_seconds': 3600},
                      {'title': '预告：大摩宏观策略谈', 'published_at': timezone.now(), 'duration_seconds': 100}]}
        payload = {'kind': 'podcast', 'url': 'https://example.com/feed.xml', 'name': '宏观访谈',
                   'include_terms': '大摩宏观策略谈', 'include_mode': 'any', 'exclude_terms': '预告',
                   'min_duration_minutes': '20', 'auto_process': 'on'}
        preview = self.client.post(reverse('intelligence:program_source_new'), {**payload, 'action': 'preview'})
        self.assertEqual(preview.status_code, 200)
        self.assertContains(preview, '✓ 命中')
        self.assertFalse(ProgramSubscription.objects.filter(family=self.family).exists())
        saved = self.client.post(reverse('intelligence:program_source_new'), {**payload, 'action': 'save'})
        self.assertEqual(saved.status_code, 302)
        source = ProgramSubscription.objects.get(family=self.family)
        self.assertTrue(source.auto_process)
        self.assertEqual(source.min_duration_seconds, 1200)
        self.assertEqual(source.exclude_terms, '预告')
        self.assertContains(self.client.get(reverse('intelligence:program_settings')), '宏观访谈')
        self.assertContains(self.client.get(reverse('intelligence:program_list')), '宏观访谈')
        inspect.return_value = {**inspect.return_value,
            'source_url': 'https://other.example.com/feed.xml',
            'feed_url': 'https://other.example.com/feed.xml'}
        changed = self.client.post(reverse('intelligence:program_source_edit', args=[source.pk]),
            {**payload, 'url': 'https://other.example.com/feed.xml', 'action': 'save'})
        self.assertEqual(changed.status_code, 302)
        source.refresh_from_db()
        self.assertIsNotNone(source.removed_at)
        self.assertFalse(source.enabled)
        replacement = ProgramSubscription.objects.filter(family=self.family).exclude(pk=source.pk).get()
        self.assertEqual(replacement.source_url, 'https://other.example.com/feed.xml')
        self.assertEqual(self.client.get(reverse('intelligence:program_source_edit', args=[source.pk])).status_code, 404)
        source = replacement
        self.client.post(reverse('intelligence:program_source_edit', args=[source.pk]), {'action': 'toggle'})
        self.assertFalse(ProgramSubscription.objects.get(pk=source.pk).enabled)

    def test_filter_title_weekday_duration(self):
        source = ProgramSubscription(family=self.family, kind='youtube', include_terms='大摩,宏观',
            include_mode='all', exclude_terms='预告', publish_weekday=0, min_duration_seconds=1200)
        monday = timezone.now() - timedelta(days=timezone.localtime(timezone.now()).weekday())
        monday = monday.replace(hour=3, minute=0)
        self.assertTrue(matches_filters(source, {'title': '大摩 宏观策略谈',
            'published_at': monday, 'duration_seconds': 2400}))
        self.assertFalse(matches_filters(source, {'title': '大摩 宏观预告',
            'published_at': monday, 'duration_seconds': 2400}))
        self.assertFalse(matches_filters(source, {'title': '大摩 宏观策略谈',
            'published_at': monday, 'duration_seconds': 100}))

    @patch('intelligence.program_network.fetch_user_source_url')
    def test_custom_rss_collects_only_matching_new_article(self, fetch):
        source = ProgramSubscription.objects.create(family=self.family, code='custom_rss', kind='article',
            custom_name='宏观文章', source_url='https://example.com/feed', feed_url='https://example.com/feed',
            include_terms='宏观', exclude_terms='预告', auto_process=True)
        published = (timezone.now() + timedelta(days=1)).strftime('%a, %d %b %Y %H:%M:%S GMT')
        fetch.return_value = SimpleNamespace(body=(
            f'<rss><channel><item><title>宏观策略</title><link>https://example.com/a</link>'
            f'<pubDate>{published}</pubDate></item><item><title>预告：宏观策略</title>'
            f'<link>https://example.com/b</link><pubDate>{published}</pubDate></item></channel></rss>').encode())
        with patch('intelligence.program_custom_sources.check_public_url', side_effect=lambda url: url):
            self.assertEqual(collect_subscription(source), 1)
        entry = ProgramEntry.objects.get()
        self.assertTrue(entry.requested)
        self.assertEqual(entry.title, '宏观策略')

    @patch('intelligence.program_custom_sources._yt_command')
    def test_bilibili_public_channel_listing(self, command):
        command.return_value = json.dumps({'entries': [{'id': 'BV1GJ411x7h7',
            'title': '大摩宏观策略谈', 'duration': 1800, 'upload_date': '20260921'}]}).encode()
        source = ProgramSubscription(kind='bilibili', code='custom_bili',
            custom_name='财经 UP 主', source_url='https://space.bilibili.com/12345/video', channel_id='12345')
        items = video_items(source)
        self.assertEqual(items[0]['url'], 'https://www.bilibili.com/video/BV1GJ411x7h7')
        self.assertEqual(items[0]['duration_seconds'], 1800)
        self.assertFalse(command.call_args.kwargs['use_proxy'])

    @patch('intelligence.program_views.inspect_bilibili_video')
    def test_manual_bilibili_video_uses_worker_without_failing_channel_collection(self, inspect):
        inspect.return_value = {'external_id': 'BV1GJ411x7h7',
            'url': 'https://www.bilibili.com/video/BV1GJ411x7h7', 'title': '公开视频',
            'duration_seconds': 1800, 'published_at': timezone.now(),
            'channel_id': '486906719', 'channel_name': '公开 UP 主'}
        response = self.client.post(reverse('intelligence:program_add_bilibili'),
                                    {'url': inspect.return_value['url']})
        self.assertEqual(response.status_code, 302)
        source = ProgramSubscription.objects.get(family=self.family)
        self.assertEqual(source.kind, 'bilibili')
        self.assertFalse(source.collect_enabled)
        self.assertEqual(collect_subscription(source), 0)
        self.assertTrue(ProgramEntry.objects.get().requested)
        manage_url = reverse('intelligence:program_source_edit', args=[source.pk])
        self.assertContains(self.client.get(manage_url), '只能逐期添加公开视频')
        self.assertContains(self.client.get(manage_url), '保存改动')
        self.client.post(manage_url, {'action': 'toggle'})
        self.assertFalse(ProgramSubscription.objects.get(pk=source.pk).enabled)
        self.client.post(manage_url, {'action': 'delete'})
        self.assertEqual(self.client.post(reverse('intelligence:program_add_bilibili'),
                                          {'url': inspect.return_value['url']}).status_code, 302)
        self.assertEqual(ProgramSubscription.objects.filter(family=self.family, kind='bilibili').count(), 2)
        self.assertEqual(ProgramEntry.objects.filter(subscription__family=self.family).count(), 2)

    def test_catalogue_source_uses_same_edit_delete_controls_and_preserves_entries(self):
        source = ProgramSubscription.objects.create(family=self.family, code='oaktree',
            custom_name='Howard Marks · Oaktree Memos',
            source_url='https://www.oaktreecapital.com/insights')
        entry = ProgramEntry.objects.create(subscription=source, external_id='memo-1',
            title='一篇备忘录', url='https://www.oaktreecapital.com/insights/memo/one')
        settings_url = reverse('intelligence:program_settings')
        page = self.client.get(settings_url)
        self.assertContains(page, 'Howard Marks · Oaktree Memos')
        self.assertContains(page, reverse('intelligence:program_source_edit', args=[source.pk]))
        self.assertContains(page, '<input type="hidden" name="action" value="delete">', html=True)
        payload = {'kind': 'article', 'url': source.source_url, 'name': 'Howard Marks 备忘录',
                   'include_terms': '风险', 'include_mode': 'any', 'exclude_terms': '',
                   'min_duration_minutes': '0', 'auto_process': 'on', 'action': 'save'}
        self.assertEqual(self.client.post(reverse('intelligence:program_source_edit', args=[source.pk]),
                                          payload).status_code, 302)
        source.refresh_from_db()
        self.assertEqual(source.custom_name, 'Howard Marks 备忘录')
        self.assertEqual(source.include_terms, '风险')
        self.assertEqual(source.kind, '')  # Keep the official memo parser.
        self.assertContains(self.client.get(settings_url), 'Howard Marks 备忘录')
        self.assertEqual(self.client.post(reverse('intelligence:program_source_edit', args=[source.pk]),
                                          {'action': 'delete'}).status_code, 302)
        source.refresh_from_db()
        self.assertFalse(source.enabled)
        self.assertIsNotNone(source.removed_at)
        self.assertEqual(source.removed_by, self.owner)
        self.assertNotContains(self.client.get(settings_url), 'Howard Marks 备忘录')
        self.assertEqual(ProgramEntry.objects.get(pk=entry.pk).subscription_id, source.pk)
        self.assertContains(self.client.get(reverse('intelligence:program_detail', args=[entry.pk])),
                            'Howard Marks 备忘录')

    @patch('intelligence.program_views.catalogue_recent_items')
    def test_catalogue_preview_does_not_save_or_request_processing(self, recent):
        source = ProgramSubscription.objects.create(family=self.family, code='dwarkesh',
            custom_name='Dwarkesh Podcast', source_url='https://www.dwarkesh.com/feed',
            auto_process=False)
        recent.return_value = [{'title': 'AI 深度访谈', 'url': 'https://www.dwarkesh.com/p/one',
                                'published_at': timezone.now(), 'duration_seconds': 3600}]
        response = self.client.post(reverse('intelligence:program_source_edit', args=[source.pk]),
            {'kind': 'article', 'url': source.source_url, 'name': 'Dwarkesh Podcast',
             'include_terms': 'AI', 'include_mode': 'any', 'action': 'preview'})
        self.assertContains(response, '✓ 命中')
        source.refresh_from_db()
        self.assertEqual(source.include_terms, '')
        self.assertFalse(source.auto_process)
        self.assertFalse(ProgramEntry.objects.exists())

    @patch('intelligence.program_sources.catalogue_recent_items')
    def test_catalogue_edit_filter_is_used_by_background_collection(self, recent):
        source = ProgramSubscription.objects.create(family=self.family, code='dwarkesh',
            include_terms='微软', auto_process=False)
        recent.return_value = [
            {'external_id': 'one', 'title': '微软访谈', 'url': 'https://www.dwarkesh.com/p/one',
             'published_at': timezone.now()},
            {'external_id': 'two', 'title': '其他访谈', 'url': 'https://www.dwarkesh.com/p/two',
             'published_at': timezone.now()},
        ]
        self.assertEqual(collect_subscription(source), 1)
        self.assertEqual(list(source.entries.values_list('title', flat=True)), ['微软访谈'])
        self.assertFalse(source.entries.get().requested)

    def test_duration_filter_rejects_sources_without_duration_metadata(self):
        source = ProgramSubscription.objects.create(family=self.family, code='rhino',
            custom_name='视野环球财经', source_url='https://www.youtube.com/@RhinoFinance')
        response = self.client.post(reverse('intelligence:program_source_edit', args=[source.pk]),
            {'kind': 'youtube', 'url': source.source_url, 'name': source.custom_name,
             'include_mode': 'any', 'min_duration_minutes': '20', 'action': 'save'})
        self.assertContains(response, '暂不能按时长筛选')
        self.assertEqual(ProgramSubscription.objects.get(pk=source.pk).min_duration_seconds, 0)

    def test_delete_blocks_in_flight_work_and_is_family_scoped(self):
        source = ProgramSubscription.objects.create(family=self.family, code='custom_busy',
            kind='article', custom_name='待处理来源', source_url='https://example.com/feed',
            feed_url='https://example.com/feed')
        entry = ProgramEntry.objects.create(subscription=source, external_id='one', title='待处理文章',
            url='https://example.com/one', lease_until=timezone.now() + timedelta(minutes=10))
        url = reverse('intelligence:program_source_edit', args=[source.pk])
        response = self.client.post(url, {'action': 'delete'}, follow=True)
        self.assertContains(response, '任务正在运行')
        source.refresh_from_db()
        self.assertIsNone(source.removed_at)
        entry.lease_until = None
        entry.save(update_fields=['lease_until'])
        self.client.force_login(self.other.user)
        self.assertEqual(self.client.post(url, {'action': 'delete'}).status_code, 403)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.client.force_login(self.owner.user)
        self.assertEqual(self.client.post(url, {'action': 'delete'}).status_code, 302)
        self.assertEqual(ProgramEntry.objects.get(pk=entry.pk).title, '待处理文章')

    @patch('intelligence.program_custom_sources.check_public_url', side_effect=lambda url: url)
    @patch('intelligence.program_custom_sources._yt_command')
    def test_bilibili_partial_second_rounds_up_for_budget(self, command, _check):
        command.return_value = json.dumps({'id': 'BV1GJ411x7h7', 'uploader_id': '12345',
            'title': '公开节目', 'duration': 212.393}).encode()
        from .program_custom_sources import inspect_bilibili_video
        item = inspect_bilibili_video('https://www.bilibili.com/video/BV1GJ411x7h7', 180)
        self.assertEqual(item['duration_seconds'], 213)

    def test_private_transcript_upload_and_archive(self):
        transcript = b'WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nMicrosoft revenue rose.\n\n00:00:03.000 --> 00:00:04.000\nCheck margins.'
        with tempfile.TemporaryDirectory() as directory, self.settings(MEDIA_ROOT=directory):
            response = self.client.post(reverse('intelligence:program_upload'), {
                'title': '会员节目笔记', 'file': SimpleUploadedFile('episode.vtt', transcript,
                                                               content_type='text/vtt')})
            self.assertEqual(response.status_code, 302)
            entry = ProgramEntry.objects.get()
            self.assertEqual(entry.private_owner_id, self.owner.pk)
            self.assertFalse(entry.allow_cloud_summary)
            self.assertEqual(entry.state, 'ready')
            self.assertEqual(entry.current_revision.segments[0]['start_ms'], 1000)
            self.assertContains(self.client.get(reverse('intelligence:program_detail', args=[entry.pk])),
                                '仅上传者可见')
            original = self.client.get(reverse('intelligence:program_uploaded_original', args=[entry.pk]))
            self.assertEqual(b''.join(original.streaming_content), transcript)
            archived = self.client.post(reverse('intelligence:program_action', args=[entry.pk]),
                {'action': 'archive', 'revision_id': entry.current_revision_id})
            self.assertEqual(archived.status_code, 302)
            self.assertEqual(KnowledgeDocument.objects.get().visibility, KnowledgeVisibility.PRIVATE)
            self.client.force_login(self.other.user)
            self.assertEqual(self.client.get(reverse('intelligence:program_detail', args=[entry.pk])).status_code, 404)
            self.assertEqual(self.client.get(reverse('intelligence:program_uploaded_original', args=[entry.pk])).status_code, 404)

    def test_audio_upload_requires_consent_and_checks_duration(self):
        buffer = io.BytesIO()
        with wave.open(buffer, 'wb') as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(8000)
            audio.writeframes(b'\x00\x00' * 8000)
        body = buffer.getvalue()
        with tempfile.TemporaryDirectory() as directory, self.settings(MEDIA_ROOT=directory):
            rejected = self.client.post(reverse('intelligence:program_upload'), {
                'title': '音频', 'file': SimpleUploadedFile('episode.wav', body, content_type='audio/wav')})
            self.assertContains(rejected, '音视频转写需要本次明确授权')
            self.assertFalse(ProgramEntry.objects.exists())
            accepted = self.client.post(reverse('intelligence:program_upload'), {
                'title': '音频', 'allow_asr': 'on',
                'file': SimpleUploadedFile('episode.wav', body, content_type='audio/wav')})
            self.assertEqual(accepted.status_code, 302)
            entry = ProgramEntry.objects.get()
            self.assertEqual(entry.duration_seconds, 1)
            self.assertEqual(entry.audio_mime, 'audio/wav')
            self.assertEqual(entry.private_owner_id, self.owner.pk)
