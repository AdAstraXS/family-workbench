"""Official IR discovery. Shared adapters use the same data the public sites display."""
import re
import json
from pathlib import Path
from dataclasses import dataclass, field
from datetime import date, datetime
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlsplit
from xml.etree import ElementTree

from bs4 import BeautifulSoup

from .ir_http import IRClient, IRError, official_url

QUARTERS = {'first': 1, 'second': 2, 'third': 3, 'fourth': 4}
FORMATS = ('.pdf', '.docx', '.pptx', '.xlsx')


def parse_date(value):
    value = str(value or '').strip()
    for fmt, size in (('%Y-%m-%d', 10), ('%m/%d/%Y', 10), ('%Y.%m.%d', 10),
                      ('%B %d, %Y', None), ('%b %d, %Y', None)):
        try:
            return datetime.strptime(value[:size] if size else value, fmt).date()
        except ValueError:
            pass
    return None


def fiscal_period(text):
    text = unquote(str(text))
    for pattern in (r'(?:FY\s*)?(20\d{2})[\s/_-]*Q([1-4])', r'Q([1-4])[^\d]{0,12}(20\d{2})',
                    r'([1-4])Q(\d{2})(?!\d)'):
        m = re.search(pattern, text, re.I)
        if m:
            a, b = map(int, m.groups())
            return (a, b) if a > 2000 else (b if b > 2000 else b + 2000, a)
    m = re.search(r'\b(first|second|third|fourth)[ -]quarter.*?\b(20\d{2})', text, re.I)
    if m:
        return int(m[2]), QUARTERS[m[1].lower()]
    return None


def material_type(label, url='', category=''):
    text = ' '.join((str(label), str(category), unquote(url))).lower()
    if re.search(r'\b10[- ]?[kq]\b|\b8[- ]?k\b|\b20[- ]?f\b|proxy|xbrl|\.zip\b', text):
        return None
    if re.search(r'transcript|transcription', text):
        return 'transcript'
    if re.search(r'prepared remarks|cfo commentary|management.?report', text):
        return 'prepared_remarks'
    if re.search(r'shareholder.?letter|letter to shareholders|founder.?letter', text):
        return 'shareholder_letter'
    if re.search(r'financial.?statement|financial.?table|income statement|balance sheet|revenue.?trend|non-gaap financial|cash.?flow', text):
        return 'financial_statements'
    if re.search(r'earnings.?release|press.?release|earnings.?update|reports?.*quarter.*results|announces?.*quarter.*results|news-pdf', text) or category.lower() == 'news':
        return 'earnings_release'
    if re.search(r'presentation|slides|update.?deck|shareholder.?deck', text):
        return 'presentation'
    if 'annual report' in text:
        return 'annual_report'
    return None


@dataclass
class IRMaterial:
    url: str
    title: str
    document_type: str
    fiscal_year: int | None
    quarter: int | None
    discovered_from: str
    published_at: date | None = None
    period_end: date | None = None
    metadata: dict = field(default_factory=dict)


@dataclass
class IRDiscovery:
    materials: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    directory_error: str = ''

    @property
    def periods(self):
        return sorted({(m.fiscal_year, m.quarter) for m in self.materials
                       if m.fiscal_year and m.quarter}, reverse=True)


class OfficialIRProvider:
    def __init__(self, company, *, client=None, today=None):
        self.company = company
        self.client = client or IRClient(company)
        self.today = today or date.today()
        self.result = IRDiscovery()

    def _soup(self, url):
        response = self.client.get(url)
        return BeautifulSoup(response.raw, 'html.parser'), response.url

    def _add(self, url, label, period, source, *, kind=None, category='', published=None,
             period_end=None, metadata=None):
        # Microsoft publishes Office viewer links pointing to the real official file.
        if urlsplit(url).hostname == 'view.officeapps.live.com':
            url = parse_qs(urlsplit(url).query).get('src', [''])[0]
        kind = kind or material_type(label, url, category)
        if not kind:
            return
        try:
            url = official_url(self.company, url, source)
        except IRError:
            return
        if re.search(r'\.(mp3|mp4|png|jpg|zip)(?:$|\?)', url, re.I):
            return
        if any(m.url == url for m in self.result.materials):
            return
        year, quarter = period or (None, None)
        period_label = f'FY{year} Q{quarter}' if year and quarter else ''
        title = f'{self.company.name} {period_label} · {label}'.strip()[:500]
        self.result.materials.append(IRMaterial(
            url, title, kind, year, quarter, source, published, period_end, metadata or {},
        ))

    def discover(self):
        try:
            getattr(self, '_' + self.company.adapter)()
        except IRError as exc:
            if not self._verified_fallback(exc):
                raise
        if not self.result.materials:
            raise IRError('官方目录未发现可归档材料，可能尚未发布或页面结构已变化。')
        selected = set(self.result.periods[:4])
        self.result.materials = [m for m in self.result.materials
                                 if not m.quarter or (m.fiscal_year, m.quarter) in selected]
        self.result.materials.sort(key=lambda m: (m.fiscal_year or 0, m.quarter or 0,
                                                  m.document_type == 'earnings_release'), reverse=True)
        if len(selected) < 4:
            self.result.warnings.append(f'官方入口目前识别到 {len(selected)} 个已发布季度，未补造更早材料。')
        return self.result

    def _verified_fallback(self, error):
        snapshots = json.loads(Path(__file__).with_name('ir_verified_links.json').read_text(encoding='utf-8'))
        snapshot = snapshots.get(self.company.key)
        if not snapshot or parse_date(snapshot['verified_at']) > self.today:
            return False
        # A dated public-link catalogue is usable even when the directory refuses
        # access. Originals still come directly from the allowed official hosts.
        self.result = IRDiscovery(directory_error=str(error))
        self.result.warnings.append(f'实时目录访问失败；目前使用 {snapshot["verified_at"]} 从官网核实的附件链接，不能确认此后新季度是否发布。')
        for item in snapshot['materials']:
            self._add(item['url'], item['title'], (item['year'], item['quarter']),
                      item.get('source', snapshot['source']), kind=item['type'],
                      published=parse_date(item.get('published_at')),
                      period_end=parse_date(item.get('period_end')),
                      metadata={**item.get('metadata', {}), 'verified_at': snapshot['verified_at'],
                                'discovery_mode': 'verified_snapshot'})
        return bool(self.result.materials)

    def _q4(self):
        origin = 'https://' + urlsplit(self.company.entry).hostname
        endpoint = origin + '/feed/FinancialReport.svc/'
        years = self.client.get(endpoint + 'GetFinancialReportYearList?LanguageId=1').json().get(
            'GetFinancialReportYearListResult')
        if not isinstance(years, list):
            raise IRError('官方财年列表格式已变化。')
        years = sorted((y for y in years if type(y) is int and 2000 <= y <= self.today.year + 1), reverse=True)
        extras = set()
        for year in years[:3]:
            url = endpoint + 'GetFinancialReportList?' + urlencode({'LanguageId': 1, 'year': year})
            rows = self.client.get(url).json().get('GetFinancialReportListResult')
            if not isinstance(rows, list):
                raise IRError('官方财报列表格式已变化。')
            for row in rows:
                subtype = str(row.get('ReportSubType', '')).lower()
                q = next((number for word, number in QUARTERS.items() if subtype == word + ' quarter'), None)
                period = (int(row['ReportYear']), q) if q else None
                for document in row.get('Documents') or []:
                    title, link = document.get('DocumentTitle', ''), document.get('DocumentPath', '')
                    kind = material_type(title, link, document.get('DocumentCategory', ''))
                    if not q and kind not in ('shareholder_letter', 'presentation'):
                        continue
                    if not q and kind in extras:
                        continue
                    before = len(self.result.materials)
                    self._add(link, title, period, url, kind=kind,
                              metadata={'directory_date': row.get('ReportDate', ''),
                                        'report_id': row.get('ReportId')})
                    if not q and len(self.result.materials) > before:
                        extras.add(kind)
            if len(self.result.periods) >= 4:
                break

    def _q4inc(self):
        soup, source = self._soup(self.company.entry)
        period, period_end = None, None
        # Both sites expose quarter headings followed by labelled links, in document order.
        for node in soup.find_all(['h2', 'h3', 'h4', 'p', 'a']):
            label = node.get_text(' ', strip=True)
            if node.name in ('h2', 'h3', 'h4'):
                found = fiscal_period(label)
                if not found and re.fullmatch(r'FY\s*20\d{2}', label):
                    found = (int(re.search(r'20\d{2}', label)[0]), 4)
                if found:
                    period, period_end = found, None
            if not period:
                continue
            if 'Ended' in label and len(label) < 80:
                period_end = parse_date(label.split('Ended')[-1].strip())
            if node.name != 'a' or not node.get('href'):
                continue
            href = node['href']
            context = label
            if label.upper() in ('PDF', 'HTML', 'XLSX'):
                context = node.parent.get_text(' ', strip=True)[:200]
                # Intel's title and format links live together in a result column.
                if material_type(context, href) is None:
                    context = node.parent.parent.get_text(' ', strip=True)[:250]
            if len(self.result.periods) >= 4 and period not in self.result.periods[:4]:
                break
            if material_type(context, href) == 'annual_report':
                continue
            self._add(href, context, period, source, period_end=period_end)

    def _microsoft(self):
        soup, source = self._soup(self.company.entry)
        matches = re.findall(r'/en-us/investor/earnings/fy-(20\d{2})-q([1-4])/press-release-webcast',
                             str(soup), re.I)
        if not matches:
            raise IRError('微软官方入口没有可识别的季度财报链接。')
        year, q = max((int(y), int(q)) for y, q in matches)
        for _ in range(4):
            url = f'https://www.microsoft.com/en-us/investor/earnings/fy-{year}-q{q}/press-release-webcast'
            page, final = self._soup(url)
            release = page.select_one('#pressreleasecontent')
            if not release or len(release.get_text(strip=True)) < 300:
                raise IRError('微软财报页缺少可核对正文，未将导航页当作财报。')
            self._add(final, 'Earnings Release', (year, q), source)
            for node in page.select('[link], a[href]'):
                target = node.get('link') or node.get('href')
                if not node.get('link') and f'/earnings/fy-{year}-q{q}/' not in target.lower():
                    continue
                self._add(target, node.get_text(' ', strip=True),
                          (year, q), final)
            year, q = (year, q - 1) if q > 1 else (year - 1, 4)

    def _apple(self):
        response = self.client.get(self.company.entry)
        if b'<!DOCTYPE' in response.raw.upper() or b'<!ENTITY' in response.raw.upper():
            raise IRError('苹果官方目录未返回有效 sitemap。')
        try:
            root = ElementTree.fromstring(response.raw)
        except ElementTree.ParseError as exc:
            raise IRError('苹果官方目录格式已变化。') from exc
        pattern = r'/newsroom/(20\d{2})/(\d{2})/apple-reports-(first|second|third|fourth)-quarter-results/?$'
        links = []
        for node in root.iter():
            if node.tag.rsplit('}', 1)[-1] != 'loc':
                continue
            match = re.search(pattern, node.text or '')
            if match and (int(match[1]), int(match[2])) <= (self.today.year, self.today.month):
                links.append((int(match[1]), int(match[2]), QUARTERS[match[3]], node.text))
        for year, month, q, url in sorted(set(links), reverse=True)[:4]:
            page, final = self._soup(url)
            self._add(final, 'Earnings Release', (year, q), response.url)
            for anchor in page.select('a[href]'):
                href = anchor['href']
                if urlsplit(href).path.lower().endswith('.pdf'):
                    self._add(href, 'Financial Statements',
                              (year, q), final, kind='financial_statements')

    def _tsmc(self):
        year, q = self.today.year, (self.today.month - 1) // 3 + 1
        for _ in range(7):
            url = f'https://investor.tsmc.com/english/quarterly-results/{year}/q{q}'
            try:
                page, source = self._soup(url)
            except IRError as exc:
                if exc.status != 404:
                    raise
                year, q = (year, q - 1) if q > 1 else (year - 1, 4)
                continue
            for node in page.select('a[href]'):
                href = node['href']
                if '/encrypt_file/reports/' in href and urlsplit(href).path.lower().endswith('.pdf'):
                    self._add(href, node.get_text(' ', strip=True), (year, q), source)
            if len(self.result.periods) >= 4:
                break
            year, q = (year, q - 1) if q > 1 else (year - 1, 4)

    def _skhynix(self):
        source = 'https://homeapi.skhynix.com/board/list?bcode=105&lang=ENG&page=1&pageSize=4'
        data = self.client.get(source).json()
        if not isinstance(data.get('list'), list):
            raise IRError('SK 海力士官方财报列表格式已变化。')
        for row in data['list']:
            period = fiscal_period(row.get('title', ''))
            event_date = parse_date(str(row.get('eventDate', '')).split('/')[0].strip())
            if not period or (event_date and event_date > self.today):
                continue
            if row.get('prLink'):
                self._add(row['prLink'], row['title'], period, source,
                          kind='earnings_release', metadata={'event_date': str(event_date or '')})
            for n in range(2, 6):
                if row.get(f'fileUrl{n}') and row.get(f'fileName{n}'):
                    url = str(data.get('cdnUrl', '')).rstrip('/') + row[f'fileUrl{n}']
                    self._add(url, row[f'fileName{n}'], period, source,
                              kind='presentation' if n == 2 else None,
                              metadata={'event_date': str(event_date or '')})

    def _broadcom(self):
        soup, source = self._soup(self.company.entry)
        home, home_url = self._soup('https://investors.broadcom.com/')
        releases = {}
        for page, base in ((home, home_url), (soup, source)):
            for anchor in page.select('a[href]'):
                label, href = anchor.get_text(' ', strip=True), anchor['href']
                period = fiscal_period(label)
                if period and 'financial results' in label.lower() and '/news-release' in href:
                    releases[period] = (href, label, base)
                if 'company presentation' in label.lower():
                    self._add(href, label, None, base)
        for period in sorted(releases, reverse=True)[:4]:
            href, label, base = releases[period]
            self._add(href, label, period, base, kind='earnings_release')

    def _tesla(self):
        soup, source = self._soup(self.company.entry)
        for row in soup.select('tr'):
            text = row.get_text(' ', strip=True)
            match = re.search(r'\b(20\d{2})\b.*?\bQ([1-4])\b', text)
            period = (int(match[1]), int(match[2])) if match else fiscal_period(text)
            for anchor in row.select('a[href]'):
                label, href = anchor.get_text(' ', strip=True), anchor['href']
                # Tesla's release column is production/deliveries, not earnings.
                if re.search(r'(Update|Shareholder)[^/]*\.pdf', href, re.I):
                    if link_period := (period or fiscal_period(href)):
                        self._add(href, 'Shareholder Deck', link_period, source, kind='presentation')
        if not self.result.materials:
            # Modern layouts use quarter cards instead of a table. Read the nearest
            # quarter container; never assign a quarter based on document order alone.
            for anchor in soup.select('a[href]'):
                href, label = anchor['href'], anchor.get_text(' ', strip=True)
                period = fiscal_period(href)
                if not period:
                    for parent in list(anchor.parents)[:4]:
                        text = parent.get_text(' ', strip=True)
                        if len(text) < 1500 and (period := fiscal_period(text)):
                            break
                if period:
                    if re.search(r'(Update|Shareholder)[^/]*\.pdf', href, re.I):
                        self._add(href, 'Shareholder Deck', period, source, kind='presentation')
