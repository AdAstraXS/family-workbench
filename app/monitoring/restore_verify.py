"""Read-only checks against a database already restored in an isolated container."""
import hashlib
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from django.apps import apps
from django.conf import settings
from django.db import connection
from django.db.models import F

BASELINE_MODELS = ('portfolio.InvestmentAccount', 'portfolio.InvestmentPosition',
                   'portfolio.InvestmentTransaction', 'portfolio.PortfolioSnapshot',
                   'portfolio.PortfolioSnapshotPositionLine', 'portfolio.DailyPortfolioValuationRun')


def row(key, checked=0, failed=0, *, unverified=False):
    return {'key': key, 'checked': checked, 'failed': failed,
            'status': 'unverified' if unverified else 'failed' if failed else 'passed'}


def checked_file(root, name, expected='', *, decrypt_key=None):
    """Reject escaped/symlink paths from restored records before opening a file."""
    root = Path(root).resolve()
    path = (root / name).resolve()
    if not name or path == root or root not in path.parents or not path.is_file():
        return False
    digest = hashlib.sha256()
    try:
        if decrypt_key is not None:
            body = Fernet(decrypt_key).decrypt(path.read_bytes())
            digest.update(body)
        else:
            with path.open('rb') as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b''):
                    digest.update(chunk)
        return not expected or digest.hexdigest() == expected
    except (OSError, ValueError, InvalidToken):
        return False


def verify_baseline(expected):
    failed = 0
    if not isinstance(expected, dict) or set(expected) != set(BASELINE_MODELS) | {'latest_snapshot_date'}:
        raise ValueError('基线必须包含规定的六张财务表计数及最新快照日期。')
    for label in BASELINE_MODELS:
        count = expected[label]
        if type(count) is not int or count < 0:
            raise ValueError('基线计数无效。')
        failed += apps.get_model(label).objects.count() != count
    latest = apps.get_model('portfolio.PortfolioSnapshot').objects.order_by('-snapshot_date').values_list('snapshot_date', flat=True).first()
    failed += (str(latest) if latest else None) != expected['latest_snapshot_date']
    return row('baseline', len(BASELINE_MODELS) + 1, failed)


def verify_references():
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*), count(*) FILTER (WHERE NOT convalidated) FROM pg_constraint WHERE contype='f'")
        checked, failed = cursor.fetchone()
    for label, relation in [('knowledge.KnowledgeDocument', 'document'), ('intelligence.ProgramEntry', 'entry')]:
        objects = apps.get_model(label).objects.filter(current_revision__isnull=False)
        checked += objects.count()
        failed += objects.exclude(**{f'current_revision__{relation}_id': F('pk')}).count()
    return row('references', checked, failed)


def verify_files(root, media_root):
    if root is None or media_root is None:
        return row('files', unverified=True)
    candidates = []
    for label, file_field, hash_field, filters in [
        ('knowledge.KnowledgeRevision', 'raw_file', 'content_hash', {'purged_at__isnull': True}),
        ('knowledge.KnowledgeAsset', 'file', 'content_hash', {'revision__purged_at__isnull': True}),
        ('knowledge.KnowledgeArtifactVersion', 'original_file', 'content_hash', {}),
        ('knowledge.KnowledgeArtifactVersion', 'rendered_file', '', {}),
    ]:
        fields = [file_field] + ([hash_field] if hash_field else [])
        for values in apps.get_model(label).objects.filter(**filters).exclude(**{file_field: ''}).values_list(*fields).iterator():
            candidates.append((root, values[0], values[1] if hash_field else '', None))
    for book in apps.get_model('reading.BookFile').objects.all().iterator():
        candidates.append((Path(root) / 'reading', book.original_path, book.sha256, None))
        if book.normalized_path:
            candidates.append((Path(root) / 'reading', book.normalized_path, '', None))
    for entry in apps.get_model('intelligence.ProgramEntry').objects.exclude(uploaded_original='').iterator():
        key = settings.KNOWLEDGE_TOKEN_ENCRYPTION_KEY.strip().encode()
        if not key:
            return row('files', unverified=True)
        candidates.append((media_root, entry.uploaded_original.name, entry.uploaded_sha256, key))
    failed = sum(not checked_file(*candidate[:3], decrypt_key=candidate[3]) for candidate in candidates)
    return row('files', len(candidates), failed)


def verify_decryption():
    key = settings.KNOWLEDGE_TOKEN_ENCRYPTION_KEY.strip()
    if not key:
        return row('decryption', unverified=True)
    from knowledge.crypto import decrypt_json
    checked = failed = 0
    for label, field in [('knowledge.SourceConnection', 'encrypted_token_cache'),
                          ('intelligence.ProgramSettings', 'encrypted_credentials'),
                          ('monitoring.BalanceAccount', 'encrypted_credentials')]:
        for value in apps.get_model(label).objects.exclude(**{field: ''}).values_list(field, flat=True).iterator():
            checked += 1
            try:
                decrypt_json(value)
            except Exception:
                # Never log decrypted data, ciphertext or provider exceptions.
                failed += 1
    return row('decryption', checked, failed, unverified=not checked)
