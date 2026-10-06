"""Exercise only the new fixed command boundary with a fake Docker/backup backend."""
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SOURCE = Path(__file__).with_name('family-workbench-deploy').read_text()


def function(name):
    start = SOURCE.index(f'{name}() {{')
    return SOURCE[start:SOURCE.index('\n}\n', start) + 3]


class ClassificationWrapperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.upload = self.root / 'family-workbench-classifications-test.json'
        self.upload.write_text('{"version":1,"ledger_rules":[]}')
        self.sha = hashlib.sha256(self.upload.read_bytes()).hexdigest()
        self.log = self.root / 'calls'
        helpers = '\n'.join(function(name) for name in (
            'die', 'require_safe_label', 'require_sha256', 'require_iso_date', 'command_classifications',
        ))
        self.script = self.root / 'driver.sh'
        self.script.write_text('set -eu\numask 077\nUPLOAD_DIR="$TEST_ROOT"\nBACKUP_DIR="$TEST_ROOT"\n' + helpers + '''
dc() {
    printf 'dc' >> "$TEST_LOG"
    printf ' [%s]' "$@" >> "$TEST_LOG"
    printf '\\n' >> "$TEST_LOG"
    [ "${MOCK_COMMAND_FAIL:-0}" = 0 ] || return 19
    cat >/dev/null
    printf '{"digest":"mock"}\\n'
}
command_database_baseline() { printf 'baseline\\n' >> "$TEST_LOG"; }
command_backup_db() {
    printf 'backup\\n' >> "$TEST_LOG"
    [ "${MOCK_BACKUP_FAIL:-0}" = 0 ] || return 18
}
command_classifications "$@"
''')

    def run_command(self, action='preview', overrides=None, env=None):
        args = ['1', '2024-01-01', '2026-10-06', self.upload.name, self.sha]
        if action == 'apply':
            args.append('a' * 64)
        for index, value in (overrides or {}).items():
            args[index] = value
        return subprocess.run(['/bin/sh', str(self.script), action, *args], capture_output=True, text=True,
            env={**os.environ, 'TEST_ROOT':str(self.root), 'TEST_LOG':str(self.log), **(env or {})})

    def test_preview_has_fixed_arguments_and_private_spool(self):
        result = self.run_command()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.log.read_text()
        self.assertIn('[preview_asset_classification]', calls)
        self.assertIn('[--family] [1] [--start] [2024-01-01]', calls)
        self.assertIn('[--confirmations] [-]', calls)
        self.assertNotIn('backup', calls)
        self.assertNotIn('[--apply]', calls)
        for artifact in self.root.glob('classification-*'):
            self.assertEqual(artifact.stat().st_mode & 0o777, 0o600)

    def test_apply_backs_up_before_fixed_complete_write_and_records_baselines(self):
        result = self.run_command('apply')
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.log.read_text().splitlines()
        self.assertEqual(calls[0:2], ['baseline','backup'])
        self.assertIn('[--apply] [--require-complete] [--expected-digest]', calls[2])
        self.assertEqual(calls[-1], 'baseline')

    def test_bad_family_date_filename_hash_and_digest_never_reach_backend(self):
        for index, value in ((0,'0'),(0,'1;id'),(1,'--apply'),(3,'../outside.json'),
            (3,'unapproved.json'),(4,'a' * 64),(4,'x' * 64),(5,'$(id)')):
            with self.subTest(index=index,value=value):
                result = self.run_command('apply', {index:value})
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.log.exists())

    def test_symlink_upload_is_rejected(self):
        target = self.root / 'actual.json'
        self.upload.rename(target)
        self.upload.symlink_to(target)
        self.assertNotEqual(self.run_command().returncode, 0)
        self.assertFalse(self.log.exists())

    def test_oversize_upload_is_rejected(self):
        self.upload.write_bytes(b' ' * (2097152 + 1))
        self.assertNotEqual(self.run_command().returncode, 0)
        self.assertFalse(self.log.exists())

    def test_backup_failure_prevents_any_write(self):
        result = self.run_command('apply', env={'MOCK_BACKUP_FAIL':'1'})
        self.assertEqual(result.returncode, 18)
        self.assertEqual(self.log.read_text().splitlines(), ['baseline','backup'])

    def test_management_failure_returns_nonzero_and_stops(self):
        result = self.run_command('apply', env={'MOCK_COMMAND_FAIL':'1'})
        self.assertEqual(result.returncode, 19)
        self.assertEqual(len(self.log.read_text().splitlines()), 3)


if __name__ == '__main__':
    unittest.main()
