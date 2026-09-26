"""Turn official originals into citeable text without interpreting financial values."""
import json
import io
import re
import subprocess
import sys
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .providers.ir_http import IRError
from .providers.official_ir import parse_date

EXTRACTOR_VERSION = 'official-ir-1'
MEDIA_TYPES = {'pdf': 'application/pdf', 'html': 'text/html',
               'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
               'pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
               'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'}


class AttachmentTextUnavailable(IRError):
    def __init__(self, media_type):
        super().__init__('原件已保留，但尚无法提取可引用正文：可能是图片页、损坏或超出提取限额。请下载原件核对。')
        self.media_type = media_type


def extract_material(response):
    suffix = Path(urlsplit(response.url).path).suffix.lower().lstrip('.')
    kind = 'pdf' if response.raw.startswith(b'%PDF-') else suffix
    if response.raw.startswith(b'PK') and kind not in ('docx', 'pptx', 'xlsx'):
        try:
            with zipfile.ZipFile(io.BytesIO(response.raw)) as archive:
                names = set(archive.namelist())
                kind = ('docx' if 'word/document.xml' in names else
                        'xlsx' if 'xl/workbook.xml' in names else
                        'pptx' if 'ppt/presentation.xml' in names else '')
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            raise IRError('Office 原件格式无效。') from exc
    if kind in ('pdf', 'docx', 'pptx', 'xlsx'):
        try:
            completed = subprocess.run(
                [sys.executable, str(Path(__file__).with_name('ir_extract_worker.py')), kind],
                input=response.raw, capture_output=True, timeout=40, check=False,
            )
            result = json.loads(completed.stdout)
            if completed.returncode or result.get('error'):
                raise ValueError
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            raise AttachmentTextUnavailable(MEDIA_TYPES[kind]) from exc
        result.update(media_type=MEDIA_TYPES[kind], published_at=None)
        return result
    if response.content_type not in ('text/html', 'application/xhtml+xml', ''):
        raise IRError('这份材料的文件格式暂不支持正文提取。')
    soup = BeautifulSoup(response.raw, 'html.parser')
    published = None
    for node in soup.select('meta[property="article:published_time"], meta[name="date"], time[datetime]'):
        published = parse_date(node.get('content') or node.get('datetime'))
        if published:
            break
    # ASP.NET sites wrap the full article in a form; remove controls, not the article.
    for node in soup.select('script, style, nav, header, footer, input, button, select, textarea, noscript, iframe, svg, template, [hidden], [aria-hidden="true"]'):
        node.decompose()
    body = None
    for selector in ('#pressreleasecontent', '.module-news-details', '.module-event-details', '.article-body', '.news-details',
                     '.field--name-body', '.newsbody', 'article', 'main', 'body'):
        candidates = soup.select(selector)
        if candidates:
            candidate = max(candidates, key=lambda n: len(n.get_text()))
            if len(candidate.get_text(strip=True)) >= 300:
                body = candidate
                break
    if body is None:
        raise IRError('页面没有可核对的材料正文，未将导航或空页面归档。')
    for row in body.select('tr'):
        row.replace_with('\n' + ' | '.join(cell.get_text(' ', strip=True) for cell in row.find_all(['td', 'th'], recursive=False)) + '\n')
    for node in body.find_all(['p', 'div', 'li', 'h1', 'h2', 'h3', 'h4', 'br', 'section']):
        node.insert_before('\n')
        node.insert_after('\n')
    text = '\n'.join(re.sub(r'\s+', ' ', line).strip() for line in body.get_text().splitlines() if line.strip())
    if len(text) < 300 or len(text) > 1_000_000 or re.search(r'^(access denied|just a moment|robot or human)', text, re.I):
        raise IRError('官方材料正文为空、访问受限或超过处理范围。')
    if published is None:
        # Datelines near the start, not arbitrary dates in a financial table.
        match = re.search(r'\b(?:January|February|March|April|May|June|July|August|September|October|November|December) \d{1,2}, 20\d{2}', text[:1500])
        published = parse_date(match[0]) if match else None
    return {'text': text, 'media_type': 'text/html', 'sections': [], 'published_at': published}
