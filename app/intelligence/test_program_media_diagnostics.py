import subprocess
import tempfile
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from family_core.models import Family, FamilyMember
from .program_media import _yt_command, probe_youtube_audio, youtube_audio, ProgramMediaError
from .program_models import ProgramEntry, ProgramSubscription
from .program_sources import ProgramError


class MediaFailureTests(SimpleTestCase):
    @patch('intelligence.program_media.time.sleep')
    @patch('intelligence.program_media._yt_command')
    def test_forbidden_refreshes_url_in_fresh_directory(self, command, sleep):
        from pathlib import Path
        paths = []
        def download(args, **kwargs):
            path = Path(args[args.index('-o') + 1])
            paths.append(path)
            self.assertFalse(path.exists())
            path.write_bytes(b'partial' if len(paths) == 1 else b'complete')
            if len(paths) == 1:
                raise ProgramMediaError('HTTP 403', 'forbidden')
        command.side_effect = download
        self.assertEqual(youtube_audio(SimpleNamespace(external_id='yjp8mm4tq5g'))[0], b'complete')
        self.assertNotEqual(paths[0].parent, paths[1].parent)
        self.assertTrue(all(not path.exists() for path in paths))

    @patch('intelligence.program_media.time.sleep')
    @patch('intelligence.program_media._yt_command')
    def test_retry_limit_and_nonretryable_failures(self, command, sleep):
        for reason, count in [('forbidden', 3), ('network', 3), ('login_required', 1),
                              ('rate_limit', 1), ('unavailable', 1)]:
            command.reset_mock()
            command.side_effect = ProgramMediaError('safe error', reason)
            with self.assertRaises(ProgramError):
                youtube_audio(SimpleNamespace(external_id='yjp8mm4tq5g'))
            self.assertEqual(command.call_count, count)

    @patch('intelligence.program_media.time.monotonic', side_effect=[0, 0, 54])
    @patch('intelligence.program_media._yt_command', side_effect=ProgramMediaError('HTTP 403', 'forbidden'))
    def test_retry_obeys_total_time_budget(self, command, monotonic):
        with self.assertRaises(ProgramError):
            youtube_audio(SimpleNamespace(external_id='yjp8mm4tq5g'), timeout=55)
        command.assert_called_once()

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

    @patch('intelligence.program_media._yt_command')
    def test_audio_uses_public_client_and_preserves_download_limits(self, command):
        from pathlib import Path
        def download(args, **kwargs):
            Path(args[args.index('-o') + 1]).write_bytes(b'checked-audio')
        command.side_effect = download
        body, mime = youtube_audio(SimpleNamespace(external_id='yjp8mm4tq5g'), timeout=55)
        args = command.call_args.args[0]
        self.assertEqual(args[args.index('--extractor-args') + 1], 'youtube:player_client=visionos')
        self.assertEqual(args[args.index('-f') + 1], 'bestaudio[ext=m4a]')
        self.assertEqual(args[args.index('--max-filesize') + 1], str(60 * 1024 * 1024))
        self.assertEqual(command.call_args.kwargs['timeout'], 55)
        self.assertEqual((body, mime), (b'checked-audio', 'audio/mp4'))


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
    @patch('intelligence.program_processing.cache_youtube_audio', return_value=(57234835, 3539))
    def test_admin_probe_does_not_change_entry_or_submit_asr(self, probe, asr):
        before = ProgramEntry.objects.filter(pk=self.entry.pk).values().get()
        response = self.client.post(self.url, {'action': 'probe_youtube'}, follow=True)
        self.assertContains(response, '检查成功')
        self.assertContains(response, '未提交转写')
        self.assertEqual(before, ProgramEntry.objects.filter(pk=self.entry.pk).values().get())
        self.assertEqual(self.entry.revisions.count(), 0)
        asr.assert_not_called()
        probe.assert_called_once()

    @patch('intelligence.program_processing.cache_youtube_audio')
    def test_member_cannot_probe_and_get_does_not_download(self, probe):
        self.assertEqual(self.client.get(self.url).status_code, 405)
        self.member.role = 'member'
        self.member.save()
        self.assertEqual(self.client.post(self.url, {'action': 'probe_youtube'}).status_code, 403)
        probe.assert_not_called()

    @patch('intelligence.program_processing.cache_youtube_audio')
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

    @patch('intelligence.program_processing.cache_youtube_audio', side_effect=ProgramError('YouTube 音频下载失败：HTTP 403'))
    def test_probe_failure_is_visible_and_preserves_original_error(self, probe):
        response = self.client.post(self.url, {'action': 'probe_youtube'}, follow=True)
        self.assertContains(response, 'HTTP 403')
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.last_error, '原错误')
        self.assertEqual(self.entry.state, 'failed')

    @patch('intelligence.program_media.verified_youtube_audio', return_value=(b'complete-audio', 'audio/mp4', 3539))
    @patch('intelligence.program_processing.asr_request')
    def test_probe_encrypts_reusable_audio_without_changing_task(self, asr, fetch):
        from cryptography.fernet import Fernet
        from knowledge.crypto import _fernet_key
        with tempfile.TemporaryDirectory() as directory, self.settings(MEDIA_ROOT=directory,
                KNOWLEDGE_TOKEN_ENCRYPTION_KEY=Fernet.generate_key().decode()):
            response = self.client.post(self.url, {'action': 'probe_youtube'}, follow=True)
            self.assertContains(response, '加密暂存')
            self.entry.refresh_from_db()
            with self.entry.audio_file.open('rb') as audio:
                encrypted = audio.read()
            self.assertNotIn(b'complete-audio', encrypted)
            self.assertEqual(Fernet(_fernet_key()).decrypt(encrypted), b'complete-audio')
            self.assertGreater(self.entry.audio_expires_at, timezone.now())
            self.assertEqual((self.entry.state, self.entry.last_error, self.entry.task_id), ('failed', '原错误', ''))
            self.assertIsNone(self.entry.submitted_at)
            self.assertEqual(self.entry.asr_reserved_cny, 0)
            asr.assert_not_called()
            from knowledge.crypto import encrypt_json
            from .program_models import ProgramSettings
            from .program_processing import submit_asr
            config = ProgramSettings.objects.create(family=self.family, allow_asr=True,
                encrypted_credentials=encrypt_json({'api_key': 'test-only'}))
            self.entry.duration_seconds = 3539
            with patch('intelligence.program_processing.youtube_audio') as download, patch(
                    'intelligence.program_processing.upload_asr_audio', side_effect=ProgramError('stop before cloud')) as upload:
                with self.assertRaisesMessage(ProgramError, 'stop before cloud'):
                    submit_asr(self.entry, config)
                download.assert_not_called()
                upload.assert_called_once_with(config, b'complete-audio', 'audio/mp4')

    @patch('intelligence.program_processing.store_audio')
    @patch('intelligence.program_media.verified_youtube_audio')
    def test_worker_starting_during_probe_prevents_cache_write(self, fetch, store):
        from .program_processing import cache_youtube_audio
        def started(entry):
            ProgramEntry.objects.filter(pk=entry.pk).update(lease_until=timezone.now() + timedelta(minutes=5))
            return b'audio', 'audio/mp4', 3539
        fetch.side_effect = started
        with self.assertRaisesMessage(ProgramError, '任务状态已变化'):
            cache_youtube_audio(self.entry)
        store.assert_not_called()
