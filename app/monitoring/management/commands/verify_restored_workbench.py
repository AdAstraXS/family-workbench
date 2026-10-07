import json
import os
import re
from pathlib import Path
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.utils import timezone
from monitoring.restore_verify import row, verify_baseline, verify_references, verify_files, verify_decryption


class Command(BaseCommand):
    help = '只读校验已恢复到隔离数据库的工作台，并写出无正文、无密钥的校验报告。'
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument('--baseline', required=True)
        parser.add_argument('--backup-sha256', required=True)
        parser.add_argument('--output', required=True)
        parser.add_argument('--knowledge-root')
        parser.add_argument('--media-root')

    def handle(self, *args, **options):
        digest = options['backup_sha256']
        if (connection.vendor != 'postgresql' or not re.fullmatch('[a-f0-9]{64}', digest)
                or os.getenv('RESTORE_DRILL_ISOLATED') != '1'
                or os.getenv('RESTORE_DRILL_RESTORED') != digest
                or connection.settings_dict['HOST'] not in {'127.0.0.1', 'localhost'}):
            raise CommandError('只能由隔离恢复工具在本机回环网络、实际恢复成功后调用。')
        with connection.cursor() as cursor:
            cursor.execute('SELECT current_database()')
            if cursor.fetchone()[0] != 'restore_drill_workbench':
                raise CommandError('拒绝访问非隔离演练数据库。')
        expected = json.loads(Path(options['baseline']).read_text(encoding='utf-8-sig'))
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute('SET TRANSACTION READ ONLY')
            try:
                checks = [row('database_restore', 1), verify_baseline(expected), verify_references(),
                          verify_files(options['knowledge_root'], options['media_root']), verify_decryption()]
            except ValueError as exc:
                raise CommandError(str(exc)) from None
        report = {'schema': 'workbench-restore-v1', 'completed_at': timezone.now().isoformat(),
                  'backup_sha256': digest, 'checks': checks}
        # Refuse to replace an older result; each execution has its own output.
        with Path(options['output']).open('x', encoding='utf-8') as output:
            json.dump(report, output, ensure_ascii=False, indent=2)
        self.stdout.write(json.dumps(checks, ensure_ascii=False))
        if any(item['status'] != 'passed' for item in checks):
            raise CommandError('恢复校验未完整通过；已保存报告，请按失败或未验证项处理。')
