import json
from datetime import date
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

    def handle(self, *args, **options):
        try:
            family = Family.objects.get(pk=options['family'])
            start, end = date.fromisoformat(options['start']), date.fromisoformat(options['end'])
            if options['apply']:
                if not options['expected_digest']:
                    raise CommandError('写入必须提供已确认预览的摘要 --expected-digest；操作前必须验证生产备份。')
                report = apply_classification_preview(family, start, end, options['expected_digest'])
            else:
                report = build_classification_preview(family, start, end)
        except (ValueError, ValidationError, Family.DoesNotExist) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(report, ensure_ascii=False, default=str, indent=2))
