"""Accept only compact operational results, never backup contents or credentials."""
import hashlib
import json
import re
from django.utils import timezone
from django.utils.dateparse import parse_datetime

CHECKS = {
    'database_restore': '数据库实际恢复', 'baseline': '关键表与快照基线',
    'references': '外键与当前版本关系', 'files': '原件与附件',
    'decryption': '加密资料解密',
}


def validate_report(raw):
    if len(raw) > 65536:
        raise ValueError('校验报告不能超过 64 KiB。')
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise ValueError('请输入有效的 JSON 校验报告。') from None
    if not isinstance(payload, dict) or payload.get('schema') != 'workbench-restore-v1':
        raise ValueError('报告格式或版本不支持。')
    stamp = parse_datetime(str(payload.get('completed_at', '')))
    if stamp is None or timezone.is_naive(stamp) or stamp > timezone.now() + timezone.timedelta(minutes=5):
        raise ValueError('报告完成时间无效。')
    backup = payload.get('backup_sha256', '')
    if not isinstance(backup, str) or not re.fullmatch('[a-f0-9]{64}', backup):
        raise ValueError('报告缺少有效备份 SHA-256。')
    rows = payload.get('checks')
    if not isinstance(rows, list) or len(rows) != len(CHECKS):
        raise ValueError('报告必须包含全部五项检查，未执行项须明确标记。')
    clean, names = [], set()
    for row in rows:
        if not isinstance(row, dict) or row.get('key') not in CHECKS or row['key'] in names:
            raise ValueError('检查项目重复或无效。')
        if row.get('status') not in {'passed', 'failed', 'unverified'}:
            raise ValueError('检查状态无效。')
        for key in ('checked', 'failed'):
            if type(row.get(key)) is not int or not 0 <= row[key] <= 10**12:
                raise ValueError('检查数量无效。')
        if row['failed'] > row['checked'] or (row['status'] == 'passed' and row['failed']):
            raise ValueError('检查结果与失败数量矛盾。')
        if row['status'] == 'failed' and not row['failed']:
            raise ValueError('失败项目必须记录失败数量。')
        if row['status'] == 'passed' and row['key'] != 'files' and not row['checked']:
            raise ValueError('未执行的检查不能标记通过。')
        names.add(row['key'])
        clean.append({key: row[key] for key in ('key', 'status', 'checked', 'failed')})
    normalized = {'schema': payload['schema'], 'completed_at': stamp.isoformat(), 'backup_sha256': backup, 'checks': clean}
    digest = hashlib.sha256(json.dumps(normalized, sort_keys=True).encode()).hexdigest()
    return {'completed_at': stamp, 'backup_sha256': backup, 'checks': clean, 'report_sha256': digest,
            'status': 'passed' if all(row['status'] == 'passed' for row in clean) else 'incomplete'}
