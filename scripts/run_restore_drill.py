"""Restore a supplied dump in disposable, network-isolated local containers.

No production connection, source database write, port publication or NAS sudo.
The optional encryption key is inherited through KNOWLEDGE_TOKEN_ENCRYPTION_KEY;
it is never written to disk or passed as a command-line value.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import uuid


def run(args, **kwargs):
    return subprocess.run(args, capture_output=True, timeout=kwargs.pop('timeout', 120), **kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dump', type=Path, required=True)
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--app', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--image', required=True)
    parser.add_argument('--knowledge-root', type=Path)
    parser.add_argument('--media-root', type=Path)
    args = parser.parse_args()
    for path in (args.dump, args.baseline, args.app):
        if not path.exists():
            parser.error('备份、基线或应用路径不存在。')
    for path in (args.knowledge_root, args.media_root):
        if path is not None and not path.is_dir():
            parser.error('附件恢复目录不存在。')
    if args.output_dir.exists():
        parser.error('输出目录已存在，请为本次演练使用新的独立目录。')
    with args.dump.open('rb') as backup:
        digest = hashlib.file_digest(backup, 'sha256').hexdigest()
    args.output_dir.mkdir(parents=True)
    name = 'workbench-restore-' + uuid.uuid4().hex[:12]
    report_path = args.output_dir / 'restore-report.json'
    env = {**os.environ, 'DJANGO_SECRET_KEY': uuid.uuid4().hex + uuid.uuid4().hex}
    started = restored_ok = False
    try:
        print('启动隔离 PostgreSQL：无外部网络、无公开端口。', flush=True)
        created = run(['docker', 'run', '--rm', '-d', '--name', name, '--network', 'none',
                       '-e', 'POSTGRES_HOST_AUTH_METHOD=trust', '-e', 'POSTGRES_DB=restore_drill_workbench', 'postgres:16'])
        if created.returncode:
            raise RuntimeError('隔离数据库容器启动失败。')
        started = True
        for attempt in range(60):
            ready = run(['docker', 'exec', name, 'pg_isready', '-h', '127.0.0.1', '-U', 'postgres', '-d', 'restore_drill_workbench'])
            if ready.returncode == 0:
                break
            time.sleep(1)
        else:
            raise RuntimeError('隔离数据库未就绪。')
        print('向专用演练数据库执行 pg_restore。', flush=True)
        with args.dump.open('rb') as backup:
            restored = run(['docker', 'exec', '-i', name, 'pg_restore', '--no-owner', '--no-acl', '--exit-on-error',
                            '-U', 'postgres', '-d', 'restore_drill_workbench'], stdin=backup, timeout=1800)
        if restored.returncode:
            raise RuntimeError('pg_restore 失败，未继续校验。')
        restored_ok = True
        print('恢复完成，核对关键表、引用、原件与解密能力。', flush=True)
        command = ['docker', 'run', '--rm', '--network', 'container:' + name,
                   '-e', 'DJANGO_DEBUG=False', '-e', 'DJANGO_SECRET_KEY',
                   '-e', 'DATABASE_URL=postgresql://postgres@127.0.0.1:5432/restore_drill_workbench',
                   '-e', 'RESTORE_DRILL_ISOLATED=1', '-e', 'RESTORE_DRILL_RESTORED=' + digest,
                   '-e', 'KNOWLEDGE_TOKEN_ENCRYPTION_KEY',
                   '-v', f'{args.app.resolve()}:/app:ro', '-v', f'{args.baseline.resolve()}:/drill-baseline.json:ro',
                   '-v', f'{args.output_dir.resolve()}:/drill-output', '-w', '/app']
        options = []
        for path, destination, option in [(args.knowledge_root, '/drill-knowledge', '--knowledge-root'),
                                           (args.media_root, '/drill-media', '--media-root')]:
            if path:
                command += ['-v', f'{path.resolve()}:{destination}:ro']
                options += [option, destination]
        command += [args.image, 'python', 'manage.py', 'verify_restored_workbench',
                    '--baseline', '/drill-baseline.json', '--backup-sha256', digest,
                    '--output', '/drill-output/restore-report.json', *options]
        checked = run(command, env=env, timeout=1800)
        if not report_path.exists():
            raise RuntimeError('校验命令未生成报告；请核对镜像依赖与数据库版本。')
        report = json.loads(report_path.read_text(encoding='utf-8'))
        print(json.dumps(report['checks'], ensure_ascii=False), flush=True)
        print('校验报告：' + str(report_path.resolve()), flush=True)
        return checked.returncode
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        print(str(exc) if isinstance(exc, RuntimeError) else '隔离操作超时，停止本次演练。', flush=True)
        if not report_path.exists():
            report = {'schema': 'workbench-restore-v1', 'completed_at': datetime.now(timezone.utc).isoformat(),
                      'backup_sha256': digest, 'checks': [
                          {'key': key, 'status': ('passed' if restored_ok else 'failed') if key == 'database_restore' else 'unverified',
                           'checked': 1 if key == 'database_restore' else 0, 'failed': int(not restored_ok) if key == 'database_restore' else 0}
                          for key in ('database_restore', 'baseline', 'references', 'files', 'decryption')]}
            report_path.write_text(json.dumps(report, indent=2), encoding='utf-8')
        return 1
    finally:
        if started:
            # This exact random container was created above; --rm removes only its scratch volume.
            run(['docker', 'stop', '--time', '10', name])


if __name__ == '__main__':
    raise SystemExit(main())
