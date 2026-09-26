from django.core.management.base import BaseCommand, CommandError

from investment_research.models import ResearchDossier
from investment_research.official_ir import company_security, sync_official_ir, fetch_ir_content, documents_for_security
from investment_research.providers.ir_http import IRClient, IRError
from investment_research.providers.ir_registry import BY_KEY, COMPANIES, company_for_security


class Command(BaseCommand):
    help = '同步官方 IR 最近四个已发布季度；不调用 SEC 或 AI，不创建持仓和私密判断。'

    def add_arguments(self, parser):
        scope = parser.add_mutually_exclusive_group()
        scope.add_argument('--all-supported', action='store_true')
        scope.add_argument('--company', choices=tuple(BY_KEY), action='append')
        parser.add_argument('--with-content', action='store_true')
        parser.add_argument('--refresh-content', action='store_true')
        parser.add_argument('--force', action='store_true')

    def handle(self, *args, **options):
        if options['all_supported']:
            companies = list(COMPANIES)
        elif options['company']:
            companies = [BY_KEY[key] for key in dict.fromkeys(options['company'])]
        else:
            companies = list({c.key: c for d in ResearchDossier.objects.select_related('security')
                              if (c := company_for_security(d.security))}.values())
        errors = []
        for company in companies:
            security = company_security(company)
            client = IRClient(company)
            try:
                state, count = sync_official_ir(security, client=client, force=options['force'])
                self.stdout.write(f'{company.key}: 新增 {count} 份；覆盖 {len(state.cursor.get("periods", []))} 季度')
                if state.last_error:
                    errors.append(company.key + '/实时目录')
                    self.stderr.write(f'{company.key}: {state.last_error} 使用已核实的历史附件，不能确认新季度。')
            except IRError as exc:
                errors.append(company.key)
                self.stderr.write(f'{company.key}: {exc}')
                continue
            if options['with_content']:
                documents = documents_for_security(security).filter(source__in=['official_ir', 'microsoft_ir'],
                                                                    source_url__in=state.cursor.get('urls', []))
                for document in documents:
                    if not options['refresh_content'] and document.content_versions.exists():
                        continue
                    try:
                        version, _ = fetch_ir_content(document, client=client)
                        status = f'正文 {len(version.content_text)} 字' if version.content_text else '仅保存原件，未提取可引用正文'
                        self.stdout.write(f'  {document.pk}: v{version.version_number}，{status}')
                    except IRError as exc:
                        errors.append(f'{company.key}/{document.pk}')
                        self.stderr.write(f'  {document.pk}: {exc}')
        if errors:
            raise CommandError('未完成的来源/正文：' + ', '.join(errors))
