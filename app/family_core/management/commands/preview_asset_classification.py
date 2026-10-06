import json
import sys
from datetime import date
from pathlib import Path
from django.core.management.base import BaseCommand, CommandError
from django.core.exceptions import ValidationError
from family_core.models import Family
from family_core.classification_preview import build_classification_preview, apply_classification_preview

class Command(BaseCommand):
    help = '只读预览资产分类；写入需显式日期、--apply 与已核对的 --expected-digest。'

    def add_arguments(self, parser):
        parser.add_argument('--family', type=int, required=True)
        parser.add_argument('--start', required=True)
        parser.add_argument('--end', required=True)
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--expected-digest')
        parser.add_argument('--confirmations', help='经核对的账本确认 JSON 文件，- 表示标准输入；默认仍为只读预览。')
        parser.add_argument('--require-complete', action='store_true', help='存在待确认项目时拒绝整批写入。')

    def handle(self, *args, **options):
        try:
            family = Family.objects.get(pk=options['family'])
            start, end = date.fromisoformat(options['start']), date.fromisoformat(options['end'])
            confirmations = None
            if options['confirmations']:
                limit = 2 * 1024 * 1024
                if options['confirmations'] == '-':
                    payload = sys.stdin.read(limit + 1)
                else:
                    path = Path(options['confirmations'])
                    if path.stat().st_size > limit:
                        raise CommandError('确认文件过大，请核对文件。')
                    payload = path.read_text(encoding='utf-8')
                if len(payload.encode('utf-8')) > limit:
                    raise CommandError('确认文件过大，请核对文件。')
                confirmations = json.loads(payload)
            if options['apply']:
                if not options['expected_digest']:
                    raise CommandError('写入必须提供已确认预览的摘要 --expected-digest；操作前必须验证生产备份。')
                report = apply_classification_preview(family, start, end, options['expected_digest'],
                    confirmations=confirmations, require_complete=options['require_complete'])
            else:
                report = build_classification_preview(family, start, end, confirmations=confirmations)
        except (OSError, ValueError, ValidationError, Family.DoesNotExist) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(report, ensure_ascii=False, default=str, indent=2))
