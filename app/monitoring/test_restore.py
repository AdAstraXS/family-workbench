import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command, CommandError
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone
from family_core.models import Family, FamilyMember
from .models import RestoreVerification
from .restore_reports import CHECKS, validate_report
from .restore_verify import checked_file, verify_files


def report():
    return {'schema': 'workbench-restore-v1', 'completed_at': timezone.now().isoformat(), 'backup_sha256': 'a'*64,
            'checks': [{'key': key, 'status': 'passed', 'checked': 1, 'failed': 0} for key in CHECKS]}


class RestoreFileTests(SimpleTestCase):
    def test_hash_and_path_escape_and_decryption_fail_closed(self):
        with TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / 'allowed'
            root.mkdir()
            (base / 'outside').write_bytes(b'private')
            (root / 'plain').write_bytes(b'content')
            digest = hashlib.sha256(b'content').hexdigest()
            self.assertTrue(checked_file(root, 'plain', digest))
            self.assertFalse(checked_file(root, '../outside'))
            self.assertFalse(checked_file(root, str(base / 'outside')))
            self.assertFalse(checked_file(root, 'plain', '0'*64))
            key = Fernet.generate_key()
            (root / 'encrypted').write_bytes(Fernet(key).encrypt(b'content'))
            self.assertTrue(checked_file(root, 'encrypted', digest, decrypt_key=key))
            self.assertFalse(checked_file(root, 'encrypted', digest, decrypt_key=Fernet.generate_key()))

    def test_missing_file_copy_is_unverified(self):
        self.assertEqual(verify_files(None, None)['status'], 'unverified')

    def test_unperformed_checks_cannot_claim_success(self):
        value = report()
        value['checks'][0]['checked'] = 0
        with self.assertRaises(ValueError):
            validate_report(json.dumps(value))
        value = report()
        value['checks'][0]['status'] = 'failed'
        with self.assertRaises(ValueError):
            validate_report(json.dumps(value))

    def test_report_cannot_claim_passed_with_failed_or_missing_check(self):
        value = report()
        value['checks'][0]['failed'] = 1
        with self.assertRaises(ValueError):
            validate_report(json.dumps(value))
        value['checks'] = value['checks'][:-1]
        with self.assertRaises(ValueError):
            validate_report(json.dumps(value))
        value = report()
        value['checks'][-1]['status'] = 'unverified'
        self.assertEqual(validate_report(json.dumps(value))['status'], 'incomplete')


class RestoreRecordTests(TestCase):
    def setUp(self):
        self.family = Family.objects.create(name='恢复验证家庭')
        self.user = get_user_model().objects.create_user('restore-admin')
        self.member = FamilyMember.objects.create(family=self.family, user=self.user, role='admin', display_name='管理员')
        self.client.force_login(self.user)

    def upload(self, raw):
        return self.client.post(reverse('monitoring:restore_verifications'), {'report': SimpleUploadedFile('report.json', raw)})

    def test_report_registration_is_idempotent_and_does_not_store_extra_data(self):
        value = report() | {'secret': 'must-not-be-stored'}
        raw = json.dumps(value).encode()
        self.assertEqual(self.upload(raw).status_code, 302)
        self.assertEqual(self.upload(raw).status_code, 302)
        self.assertEqual(RestoreVerification.objects.count(), 1)
        self.assertNotIn('must-not-be-stored', str(RestoreVerification.objects.values().first()))
        response = self.client.get(reverse('monitoring:index'))
        self.assertContains(response, '隔离恢复验证')

    def test_member_can_read_but_not_register(self):
        self.member.role = 'member'
        self.member.save(update_fields=['role'])
        self.assertEqual(self.client.get(reverse('monitoring:restore_verifications')).status_code, 200)
        self.assertEqual(self.upload(json.dumps(report()).encode()).status_code, 403)
        self.assertEqual(RestoreVerification.objects.count(), 0)

    def test_verifier_rejects_regular_database(self):
        with self.assertRaises(CommandError):
            call_command('verify_restored_workbench', baseline='unused', backup_sha256='a'*64, output='unused')
