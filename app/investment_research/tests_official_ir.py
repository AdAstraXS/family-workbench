import gzip
import hashlib
import io
import json
from datetime import date, timedelta
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from family_core.models import Family, FamilyMember
from portfolio.models import Security, InvestmentPosition
from .citations import locate_quote, resolve_quote
from .ir_extraction import extract_material
from .models import OfficialResearchDocument, OfficialResearchContentVersion, ResearchDossier, ResearchSourceState
from .official_ir import company_security, documents_for_security, fetch_ir_content, sync_official_ir
from .providers.ir_http import IRClient, IRError, IRResponse, official_url, _Redirect
from .providers.ir_registry import BY_KEY, COMPANIES, company_for_security
from .providers.official_ir import OfficialIRProvider, IRMaterial, IRDiscovery, material_type


class FakeClient:
    def __init__(self, mapping):
        self.mapping = mapping
        self.calls = []

    def get(self, url):
        self.calls.append(url)
        data = self.mapping[url]
        if isinstance(data, Exception):
            raise data
        raw = json.dumps(data).encode() if isinstance(data, (dict, list)) else data.encode()
        return IRResponse(url, raw, 'application/json' if isinstance(data, dict) else 'text/html')


class OfficialIRProviderTests(SimpleTestCase):
    def test_catalogue_identity_is_market_qualified(self):
        self.assertEqual(len(COMPANIES), 13)
        self.assertEqual(company_for_security(Security(symbol='GOOG', market='US', asset_type='stock')).key, 'alphabet')
        self.assertEqual(company_for_security(Security(symbol='000660', market='KR', asset_type='stock')).key, 'skhynix')
        self.assertIsNone(company_for_security(Security(symbol='000660', market='US', asset_type='stock')))
        self.assertIsNone(company_for_security(Security(symbol='MSFT', market='US', asset_type='option')))

    def test_official_asset_boundaries(self):
        for bad in ['https://localhost/file', 'https://s21.q4cdn.com/184289198/other.pdf',
                    'https://s21.q4cdn.com/399680738/%252e%252e/file',
                    'https://s21.q4cdn.com.evil.test/399680738/file',
                    'https://user:pass@investor.atmeta.com/file', 'file:///tmp/file',
                    'https://investor.atmeta.com:123/file', 'https://[invalid/file']:
            with self.subTest(url=bad), self.assertRaises(IRError):
                official_url(BY_KEY['meta'], bad)
        self.assertEqual(official_url(BY_KEY['meta'], '//s21.q4cdn.com/399680738/f.pdf?utm_source=x'),
                         'https://s21.q4cdn.com/399680738/f.pdf')
        with self.assertRaises(IRError):
            official_url(BY_KEY['amd'], 'https://d1io3yog0oux5.cloudfront.net/_abc/intel/db/file.pdf')

    def test_redirect_is_validated_before_network(self):
        import urllib.request
        request = urllib.request.Request('https://investor.atmeta.com/')
        with self.assertRaises(IRError):
            _Redirect(BY_KEY['meta']).redirect_request(request, None, 302, '', {}, 'http://127.0.0.1/')

    def test_size_and_compression_limits(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.geturl.return_value = 'https://investor.atmeta.com/file'
        response.headers = {'Content-Encoding': 'gzip'}
        response.read.return_value = gzip.compress(b'x'*5000)
        opener = Mock()
        opener.open.return_value = response
        with self.assertRaises(IRError):
            IRClient(BY_KEY['meta'], opener=opener, max_bytes=100, interval=0).get(response.geturl())

    def test_q4_four_quarters_and_no_invented_publication_date(self):
        company = BY_KEY['meta']
        base = 'https://investor.atmeta.com/feed/FinancialReport.svc/'
        rows = []
        for q, word in [(1, 'First'), (2, 'Second'), (3, 'Third'), (4, 'Fourth')]:
            rows.append({'ReportYear': 2025, 'ReportSubType': word+' Quarter', 'ReportDate':'12/31/2025 00:00:00',
                         'Documents':[{'DocumentTitle':'Earnings Release', 'DocumentPath':f'https://s21.q4cdn.com/399680738/q{q}.pdf'}]})
        client = FakeClient({base+'GetFinancialReportYearList?LanguageId=1':{'GetFinancialReportYearListResult':[2025]},
                             base+'GetFinancialReportList?LanguageId=1&year=2025':{'GetFinancialReportListResult':rows}})
        result = OfficialIRProvider(company, client=client).discover()
        self.assertEqual(len(result.periods), 4)
        self.assertTrue(all(m.published_at is None and m.period_end is None for m in result.materials))

    def test_skhynix_event_date_is_not_publication(self):
        url = 'https://homeapi.skhynix.com/board/list?bcode=105&lang=ENG&page=1&pageSize=4'
        data = {'cdnUrl':'https://mis-prod-koce-homepage-cdn-01-blob-ep.azureedge.net/web',
                'list':[{'title':'SK hynix FY2026 Q2 Earnings Results', 'eventDate':'Jul 29, 2026/ 9:00 AM KST',
                         'displayDate':'2026.01.01', 'prLink':'https://news.skhynix.com/q2-results/',
                         'fileName2':'Presentation.pdf', 'fileUrl2':'/attach/123.pdf'}]}
        result = OfficialIRProvider(BY_KEY['skhynix'], client=FakeClient({url:data}), today=date(2026,9,26)).discover()
        self.assertEqual(len(result.materials), 2)
        self.assertIsNone(result.materials[0].published_at)
        self.assertTrue(result.warnings)

    def test_types_do_not_conflate_remarks_and_transcript(self):
        self.assertEqual(material_type('CFO Commentary'), 'prepared_remarks')
        self.assertEqual(material_type('Transcript'), 'transcript')
        self.assertIsNone(material_type('Webcast'))
        self.assertIsNone(material_type('10-Q'))

    def test_aspnet_form_keeps_article_and_table_cells(self):
        raw = ('<body><form><nav>Navigation</nav><article><h1>Results</h1><p>'+'Revenue grew. '*40+
               '</p><table><tr><td>Revenue</td><td>100</td></tr></table></article><input value="secret"></form></body>').encode()
        result = extract_material(IRResponse('https://ir.aboutamazon.com/a',raw,'text/html'))
        self.assertIn('Revenue | 100', result['text'])
        self.assertNotIn('Navigation', result['text'])
        self.assertNotIn('secret', result['text'])

    def test_navigation_or_empty_page_rejected(self):
        with self.assertRaises(IRError):
            extract_material(IRResponse('https://abc.xyz/a',b'<body><nav>Read transcript</nav></body>','text/html'))

    def test_tesla_snapshot_is_dated_and_reports_directory_failure(self):
        client = FakeClient({'https://ir.tesla.com/':IRError('HTTP 403',status=403)})
        result = OfficialIRProvider(BY_KEY['tsla'],client=client).discover()
        self.assertEqual(len(result.periods),4)
        self.assertEqual(result.directory_error,'HTTP 403')
        self.assertIn('2026-09-26',result.warnings[0])
        self.assertTrue(all(m.metadata['discovery_mode']=='verified_snapshot' for m in result.materials))

    def test_tesla_production_release_not_mislabeled_earnings(self):
        html = '<table><tr><td>2026</td><td>Q2</td><td><a href="/deliveries">Press Release</a></td><td><a href="https://assets-ir.tesla.com/tesla-contents/IR/TSLA-Q2-2026-Update.pdf">Download</a></td></tr></table>'
        result = OfficialIRProvider(BY_KEY['tsla'],client=FakeClient({'https://ir.tesla.com/':html})).discover()
        self.assertEqual(len(result.materials),1)
        self.assertEqual(result.materials[0].document_type,'presentation')
        self.assertEqual(result.periods,[(2026,2)])

    def test_q4_refusal_uses_dated_official_links_without_retry(self):
        company = BY_KEY['meta']
        url = 'https://investor.atmeta.com/feed/FinancialReport.svc/GetFinancialReportYearList?LanguageId=1'
        client = FakeClient({url: IRError('HTTP 429', status=429)})
        result = OfficialIRProvider(company, client=client, today=date(2026,9,26)).discover()
        self.assertEqual(client.calls, [url])
        self.assertEqual(result.directory_error, 'HTTP 429')
        self.assertEqual(len(result.periods), 4)
        for material in result.materials:
            self.assertEqual(official_url(company, material.url), material.url)
            self.assertEqual(material.metadata['discovery_mode'], 'verified_snapshot')
        with self.assertRaises(IRError):
            OfficialIRProvider(company, client=client, today=date(2026,9,25)).discover()

    def test_results_page_retains_quarter_and_statement_type(self):
        company=BY_KEY['amd']
        html='<h2>Q2 2026</h2><p>Quarter Ended Jun 27, 2026</p><div><h3>Earnings Release</h3><a href="/news/q2">HTML</a></div><a href="https://d1io3yog0oux5.cloudfront.net/_abc/amd/db/123/tables.pdf">Financial Tables</a>'
        result=OfficialIRProvider(company,client=FakeClient({company.entry:html})).discover()
        self.assertEqual(result.periods,[(2026,2)])
        self.assertEqual({m.document_type for m in result.materials},{'earnings_release','financial_statements'})
        self.assertTrue(all(m.period_end==date(2026,6,27) for m in result.materials))

    def test_apple_sitemap_uses_only_published_quarters(self):
        company=BY_KEY['aapl']
        url='https://www.apple.com/newsroom/2026/07/apple-reports-third-quarter-results/'
        future='https://www.apple.com/newsroom/2026/10/apple-reports-fourth-quarter-results/'
        mapping={company.entry:f'<urlset><url><loc>{url}</loc></url><url><loc>{future}</loc></url></urlset>',url:'<a href="/newsroom/pdfs/q3.pdf">Financial Statements</a>'}
        result=OfficialIRProvider(company,client=FakeClient(mapping),today=date(2026,9,26)).discover()
        self.assertEqual(result.periods,[(2026,3)])
        self.assertEqual(len(result.materials),2)

    def test_tsmc_unpublished_current_quarter_404_can_use_prior_reports(self):
        mapping={}
        for year,q in [(2026,3),(2026,2),(2026,1),(2025,4),(2025,3)]:
            url=f'https://investor.tsmc.com/english/quarterly-results/{year}/q{q}'
            mapping[url]=(IRError('404',status=404) if (year,q)==(2026,3) else
                          f'<a href="/english/encrypt/files/encrypt_file/reports/{year}/q{q}/release.pdf">Press Release</a>')
        result=OfficialIRProvider(BY_KEY['tsmc'],client=FakeClient(mapping),today=date(2026,9,26)).discover()
        self.assertEqual(result.periods,[(2026,2),(2026,1),(2025,4),(2025,3)])

    def test_extensionless_office_file_and_pdf_page_offsets(self):
        import zipfile
        content=io.BytesIO()
        with zipfile.ZipFile(content,'w') as archive:
            archive.writestr('word/document.xml','<document><p><t>'+('Financial results. '*20)+'</t></p></document>')
        result=extract_material(IRResponse('https://cdn-dynmedia-1.microsoft.com/is/content/microsoftcorp/Transcript',content.getvalue(),'application/octet-stream'))
        self.assertIn('wordprocessingml',result['media_type'])
        section=result['sections'][0]
        self.assertTrue(result['text'][section['start']:section['end']].startswith('Financial results.'))


class OfficialIRIntegrationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.family = Family.objects.create(name='IR test family')
        cls.user = get_user_model().objects.create_user(username='ir-test')
        cls.member = FamilyMember.objects.create(user=cls.user, family=cls.family, display_name='IR member')
        cls.security = company_security(BY_KEY['alphabet'])
        cls.alias = Security.objects.create(symbol='GOOG', market='US', currency='USD', name='Alphabet')
        cls.dossier = ResearchDossier.objects.create(owner=cls.member, family=cls.family, security=cls.alias, initial_thesis='')

    def setUp(self):
        self.client.force_login(self.user)

    def material(self):
        return IRMaterial('https://s206.q4cdn.com/479360582/test.pdf','Results','earnings_release',2026,2,'https://abc.xyz/')

    def discover(self):
        with patch('investment_research.official_ir.OfficialIRProvider') as provider:
            provider.return_value.discover.return_value = IRDiscovery([self.material()])
            state, count = sync_official_ir(self.security)
        return state, count

    def test_aliases_share_documents_and_sync_is_idempotent(self):
        state, count = self.discover()
        self.assertEqual(count, 1)
        ResearchSourceState.objects.filter(pk=state.pk).update(last_checked_at=timezone.now()-timedelta(days=1), last_success_at=timezone.now()-timedelta(days=1))
        self.assertEqual(self.discover()[1], 0)
        self.assertEqual(documents_for_security(self.alias).count(), 1)
        self.assertEqual(OfficialResearchDocument.objects.count(), 1)

    def test_failure_preserves_saved_documents_and_records_error(self):
        state, _ = self.discover()
        ResearchSourceState.objects.filter(pk=state.pk).update(last_checked_at=timezone.now()-timedelta(days=1))
        with patch('investment_research.official_ir.OfficialIRProvider') as provider:
            provider.return_value.discover.side_effect = IRError('HTTP 403')
            with self.assertRaises(IRError):
                sync_official_ir(self.security, force=True)
        state.refresh_from_db()
        self.assertEqual(state.last_error, 'HTTP 403')
        self.assertEqual(OfficialResearchDocument.objects.count(), 1)

    def test_versions_and_quotes_survive_original_update(self):
        self.discover()
        doc = OfficialResearchDocument.objects.get()
        client = Mock()
        original = b'<body><article><p>Unique original quote.</p><p>'+b'Other text. '*35+b'</p></article></body>'
        client.get.return_value = IRResponse('https://abc.xyz/article', original, 'text/html')
        old, _ = fetch_ir_content(doc, client=client)
        same, created = fetch_ir_content(doc, client=client)
        self.assertFalse(created)
        self.assertEqual(same.pk, old.pk)
        a,b,digest = locate_quote(old, 'Unique original quote.')
        client.get.return_value = IRResponse('https://abc.xyz/article', original.replace(b'original', b'updated'), 'text/html')
        new, created = fetch_ir_content(doc, client=client)
        self.assertTrue(created)
        self.assertEqual(new.version_number, 2)
        self.assertEqual(resolve_quote(old,a,b,digest)[1], 'Unique original quote.')
        response = self.client.get(reverse('investment_research:original_document',args=[self.dossier.pk,doc.pk,old.pk]))
        self.assertEqual(response.content, original)
        self.assertEqual(response['Content-Type'], 'application/octet-stream')
        self.assertIn('attachment', response['Content-Disposition'])

    def test_explore_creates_korean_target_without_holding_or_thesis(self):
        response = self.client.post(reverse('investment_research:explore'),{'company':'skhynix'})
        self.assertEqual(response.status_code, 302)
        target = ResearchDossier.objects.get(security__symbol='000660')
        self.assertEqual(target.security.market,'KR')
        self.assertEqual(target.security.currency,'KRW')
        self.assertFalse(target.current_revision_id)
        self.assertEqual(InvestmentPosition.objects.count(),0)

    def test_reading_never_fetches_and_foreign_dossier_is_private(self):
        self.discover()
        doc = OfficialResearchDocument.objects.get()
        with patch('investment_research.providers.ir_http.IRClient.get', side_effect=AssertionError('No network')):
            self.assertEqual(self.client.get(reverse('investment_research:documents',args=[self.dossier.pk])).status_code,200)
            self.assertEqual(self.client.get(reverse('investment_research:document_detail',args=[self.dossier.pk,doc.pk])).status_code,200)
        other_user = get_user_model().objects.create_user(username='other-ir')
        FamilyMember.objects.create(user=other_user,family=self.family,display_name='Other')
        self.client.force_login(other_user)
        self.assertEqual(self.client.get(reverse('investment_research:document_detail',args=[self.dossier.pk,doc.pk])).status_code,404)
        self.assertEqual(self.client.post(reverse('investment_research:fetch_ir',args=[self.dossier.pk,doc.pk])).status_code,404)

    def test_csrf_and_post_required(self):
        url = reverse('investment_research:sync_ir',args=[self.dossier.pk])
        self.assertEqual(self.client.get(url).status_code,405)
        from django.test import Client
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        self.assertEqual(client.post(url).status_code,403)

    def test_catalogue_get_shows_13_without_creating_targets(self):
        before=Security.objects.count()
        response=self.client.get(reverse('investment_research:ir_catalogue'))
        self.assertEqual(len(response.context['cards']),13)
        self.assertEqual(Security.objects.count(),before)
        self.assertEqual(ResearchDossier.objects.count(),1)

    def test_directory_failure_cannot_replace_a_newer_saved_catalogue(self):
        self.discover()
        state = ResearchSourceState.objects.get()
        original = dict(state.cursor)
        state.last_checked_at = timezone.now() - timedelta(minutes=3)
        state.save()
        fallback = IRDiscovery(directory_error='HTTP 429')
        with patch('investment_research.official_ir.OfficialIRProvider.discover', return_value=fallback):
            updated, created = sync_official_ir(self.dossier.security, force=True)
        self.assertEqual(created, 0)
        self.assertEqual(updated.cursor['urls'], original['urls'])
        self.assertEqual(updated.cursor['periods'], original['periods'])
        self.assertEqual(updated.last_error, 'HTTP 429')
        self.assertEqual(OfficialResearchDocument.objects.count(), 1)

    def test_image_only_attachment_retains_original_without_ai_text(self):
        from .ir_extraction import AttachmentTextUnavailable
        self.discover()
        doc=OfficialResearchDocument.objects.get()
        client=Mock()
        client.get.return_value=IRResponse(doc.source_url,b'%PDF-example','application/pdf')
        with patch('investment_research.official_ir.extract_material',side_effect=AttachmentTextUnavailable('application/pdf')):
            version,_=fetch_ir_content(doc,client=client)
        self.assertEqual(version.content_text,'')
        self.assertEqual(gzip.decompress(version.raw_gzip),b'%PDF-example')
        response=self.client.get(reverse('investment_research:detail',args=[self.dossier.pk]))
        self.assertEqual(response.context['available_versions'],[])
