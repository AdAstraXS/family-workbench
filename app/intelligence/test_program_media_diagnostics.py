import subprocess
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from family_core.models import Family, FamilyMember
from .program_media import _yt_command, probe_youtube_audio
from .program_models import ProgramEntry, ProgramSubscription
from .program_sources import ProgramError


class MediaFailureTests(SimpleTestCase):
    @patch('intelligence.program_media.source_proxy', return_value='')
    @patch('intelligence.program_media.subprocess.run')
    def test_failure_distinguishes_stage_and_does_not_expose_signed_urls(self, run, proxy):
        cases = [
            (b'ERROR: HTTP Error 403: Forbidden', 'HTTP 403', 'forbidden'),
            (b'ERROR: HTTP Error 429: Too Many Requests', 'HTTP 429', 'rate_limit'),
            (b'ERROR: Sign in to confirm you are not a bot', '要求登录或验证', 'login_required'),
            (b'WARNING: No supported JavaScript runtime\nERROR: Requested format is not available', '音视频格式', 'format'),
            (b'ERROR: Video unavailable', '视频不可用', 'unavailable'),
            (b'ERROR: Unable to download webpage: connection reset', '连接失败', 'network'),
            (b'No module named yt_dlp', '缺少抓取工具', 'missing_tool'),
            (b'ERROR: unknown failure', '解析或下载', 'extractor'),
        ]
        for stderr, message, reason in cases:
            with self.subTest(reason=reason):
                run.side_effect = subprocess.CalledProcessError(1, ['private-command'],
                    stderr=stderr + b' https://media.example/?token=DO_NOT_LEAK')
                with self.assertLogs('intelligence.program_media', level='WARNING') as logs:
                    with self.assertRaises(ProgramError) as caught:
                        _yt_command(['-o', '/tmp/private-path/audio.m4a'])
                self.assertIn('音频下载失败', str(caught.exception))
                self.assertIn(message, str(caught.exception))
                self.assertIn('reason=' + reason, logs.output[0])
                for output in (str(caught.exception), logs.output[0]):
                    self.assertNotIn('DO_NOT_LEAK', output)
                    self.assertNotIn('private-path', output)

    @patch('intelligence.program_media.source_proxy', return_value='')
    @patch('intelligence.program_media.subprocess.run')
    def test_timeout_and_missing_executable_have_specific_errors(self, run, proxy):
        for error, expected in [(subprocess.TimeoutExpired('command', 30), '超过 30 秒'),
                                (OSError('private-path'), '无法启动')]:
            run.side_effect = error
            with self.assertRaisesMessage(ProgramError, expected):
                _yt_command(['--dump-single-json'], timeout=30)

    @patch('intelligence.program_media.source_proxy', return_value='')
    @patch('intelligence.program_media.subprocess.run')
    def test_successful_download_with_runtime_warning_remains_success(self, run, proxy):
        run.return_value = SimpleNamespace(stdout=b'{}', stderr=b'No supported JavaScript runtime')
        self.assertEqual(_yt_command(['--dump-single-json']), b'{}')
        self.assertNotIn('--no-warnings', run.call_args.args[0])

    @patch('intelligence.program_media.media_duration', return_value=3539)
    @patch('intelligence.program_media.youtube_audio', return_value=(b'audio', 'audio/mp4'))
    @patch('intelligence.program_media.youtube_metadata', return_value={'duration': 3539})
    def test_probe_is_bounded_and_checks_audio_duration(self, metadata, audio, duration):
        entry = SimpleNamespace()
        self.assertEqual(probe_youtube_audio(entry), (5, 3539))
        metadata.assert_called_once_with(entry, 240, timeout=30)
        audio.assert_called_once_with(entry, timeout=55)
        duration.return_value = 20
        with self.assertRaisesMessage(ProgramError, '时长与节目不符'):
            probe_youtube_audio(entry)


class ProbeActionTests(TestCase):
    def setUp(self):
        self.family = Family.objects.create(name='检查家庭')
        self.user = get_user_model().objects.create_user(username='probe-admin')
        self.member = FamilyMember.objects.create(family=self.family, user=self.user,
            display_name='管理员', role='admin')
        self.sub = ProgramSubscription.objects.create(family=self.family, code='custom_probe',
            kind='youtube', custom_name='公开视频')
        self.entry = ProgramEntry.objects.create(subscription=self.sub, external_id='yjp8mm4tq5g',
            title='测试节目', state='failed', last_error='原错误')
        self.url = reverse('intelligence:program_action', args=[self.entry.pk])
        self.client.force_login(self.user)

    @patch('intelligence.program_processing.asr_request')
    @patch('intelligence.program_media.probe_youtube_audio', return_value=(57234835, 3539))
    def test_admin_probe_does_not_change_entry_or_submit_asr(self, probe, asr):
        before = ProgramEntry.objects.filter(pk=self.entry.pk).values().get()
        response = self.client.post(self.url, {'action': 'probe_youtube'}, follow=True)
        self.assertContains(response, '检查成功')
        self.assertContains(response, '未提交转写')
        self.assertEqual(before, ProgramEntry.objects.filter(pk=self.entry.pk).values().get())
        self.assertEqual(self.entry.revisions.count(), 0)
        asr.assert_not_called()
        probe.assert_called_once()

    @patch('intelligence.program_media.probe_youtube_audio')
    def test_member_cannot_probe_and_get_does_not_download(self, probe):
        self.assertEqual(self.client.get(self.url).status_code, 405)
        self.member.role = 'member'
        self.member.save()
        self.assertEqual(self.client.post(self.url, {'action': 'probe_youtube'}).status_code, 403)
        probe.assert_not_called()

    @patch('intelligence.program_media.probe_youtube_audio')
    def test_foreign_entry_and_active_worker_cannot_probe(self, probe):
        other = Family.objects.create(name='其他家庭')
        sub = ProgramSubscription.objects.create(family=other, code='other', kind='youtube')
        entry = ProgramEntry.objects.create(subscription=sub, external_id='other')
        self.assertEqual(self.client.post(reverse('intelligence:program_action', args=[entry.pk]),
            {'action': 'probe_youtube'}).status_code, 404)
        self.entry.lease_until = timezone.now() + timedelta(minutes=5)
        self.entry.save()
        response = self.client.post(self.url, {'action': 'probe_youtube'}, follow=True)
        self.assertContains(response, '任务正在运行')
        probe.assert_not_called()

    @patch('intelligence.program_media.probe_youtube_audio', side_effect=ProgramError('YouTube 音频下载失败：HTTP 403'))
    def test_probe_failure_is_visible_and_preserves_original_error(self, probe):
        response = self.client.post(self.url, {'action': 'probe_youtube'}, follow=True)
        self.assertContains(response, 'HTTP 403')
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.last_error, '原错误')
        self.assertEqual(self.entry.state, 'failed')
