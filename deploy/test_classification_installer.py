"""Root-only local container tests with harmless wrappers and temporary paths."""
import hashlib
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).with_name('install-classification-entry.sh').read_text()
OLD = '#!/bin/sh\nprintf "old wrapper\\n"\n'
NEW = '#!/bin/sh\ncase "$1" in help|status) printf "verified wrapper\\n" ;; *) exit 1 ;; esac\n'
sha = lambda value: hashlib.sha256(value.encode()).hexdigest()


class ClassificationInstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.original = self.root / 'wrapper'
        self.backup = self.root / 'wrapper.before-classification-20261006'
        self.candidate = self.root / 'candidate'
        self.original.write_text(OLD)
        self.original.chmod(0o755)
        self.candidate.write_text(NEW)
        self.installer = self.root / 'install.sh'

    def run_installer(self, candidate_text=NEW, args=()):
        text = SCRIPT.replace('/usr/local/sbin/family-workbench-deploy', str(self.original))
        text = text.replace('/volume1/homes/DX/family-workbench-deploy.classification-bootstrap', str(self.candidate))
        text = text.replace('/volume1/docker/family-workbench/backups/classification-entry-install-20261006.receipt', str(self.root / 'receipt'))
        text = text.replace('/usr/local/sbin/.family-workbench-classification.', str(self.root / '.stage.'))
        text = text.replace('27b3d8008df8a8f9236c37e0c59d695d49a54563c4c563d51868383ddb520c03', sha(OLD))
        text = text.replace('0b0025d1bf58f603e33e83388c21f2a355146c64c073cfdf84ec691b6bc7ef5c', sha(candidate_text))
        self.installer.write_text(text)
        return subprocess.run(['/bin/sh', str(self.installer), *args], capture_output=True, text=True)

    def test_success_is_atomic_backed_up_private_and_idempotent(self):
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.original.read_text(), NEW)
        self.assertEqual(self.backup.read_text(), OLD)
        self.assertEqual(self.original.stat().st_mode & 0o777, 0o755)
        self.assertEqual((self.root / 'receipt').stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.run_installer().returncode, 0)
        self.assertEqual(self.backup.read_text(), OLD)
        self.assertFalse(list(self.root.glob('.stage.*')))

    def test_changed_candidate_does_not_touch_original(self):
        self.candidate.write_text('unapproved source')
        self.assertNotEqual(self.run_installer().returncode, 0)
        self.assertEqual(self.original.read_text(), OLD)
        self.assertFalse(self.backup.exists())

    def test_changed_original_is_not_overwritten(self):
        self.original.write_text('another deployment')
        self.assertNotEqual(self.run_installer().returncode, 0)
        self.assertEqual(self.original.read_text(), 'another deployment')

    def test_symlink_candidate_is_rejected(self):
        self.candidate.unlink()
        target = self.root / 'source'
        target.write_text(NEW)
        self.candidate.symlink_to(target)
        self.assertNotEqual(self.run_installer().returncode, 0)
        self.assertEqual(self.original.read_text(), OLD)

    def test_existing_different_backup_is_preserved(self):
        self.backup.write_text('different backup')
        self.assertNotEqual(self.run_installer().returncode, 0)
        self.assertEqual(self.original.read_text(), OLD)
        self.assertEqual(self.backup.read_text(), 'different backup')

    def test_failed_post_install_check_restores_old_wrapper(self):
        broken = '#!/bin/sh\nexit 7\n'
        self.candidate.write_text(broken)
        result = self.run_installer(candidate_text=broken)
        self.assertEqual(result.returncode, 7)
        self.assertEqual(self.original.read_text(), OLD)
        self.assertEqual(self.backup.read_text(), OLD)
        self.assertFalse((self.root / 'receipt').exists())

    def test_arbitrary_arguments_rejected(self):
        self.assertNotEqual(self.run_installer(args=('other-target',)).returncode, 0)
        self.assertEqual(self.original.read_text(), OLD)


if __name__ == '__main__':
    unittest.main()
