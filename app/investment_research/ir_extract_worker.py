"""Bounded public document extraction, isolated from Django and all network clients."""
import io
import json
import re
import sys
import zipfile
from xml.etree import ElementTree

MAX_CHARS = 1_000_000


def xml_text(raw):
    if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
        raise ValueError('XML entities are not supported')
    root = ElementTree.fromstring(raw)
    lines = []
    for paragraph in root.iter():
        if paragraph.tag.rsplit('}', 1)[-1] == 'p':
            line = ''.join(n.text or '' for n in paragraph.iter()
                           if n.tag.rsplit('}', 1)[-1] == 't').strip()
            if line:
                lines.append(line)
    return '\n'.join(lines)


def extract(raw, kind):
    sections = []
    if kind == 'pdf':
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(raw))
        if reader.is_encrypted and not reader.decrypt(''):
            raise ValueError('Password-protected document')
        if not 1 <= len(reader.pages) <= 200:
            raise ValueError('Encrypted or too many pages')
        for n, page in enumerate(reader.pages, 1):
            text = page.extract_text(extraction_mode='layout') or ''
            text = '\n'.join(re.sub(r'[ \t]+', ' ', line).strip() for line in text.splitlines() if line.strip())
            sections.append((f'第 {n} 页', text))
            if sum(len(t) for _, t in sections) > MAX_CHARS:
                raise ValueError('Too much text')
    else:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            infos = archive.infolist()
            if len(infos) > 3000 or sum(f.file_size for f in infos) > 80*1024*1024:
                raise ValueError('Office archive exceeds limits')
            if any(f.flag_bits & 1 for f in infos):
                raise ValueError('Encrypted archive')
            if kind == 'docx':
                sections.append(('正文', xml_text(archive.read('word/document.xml'))))
            elif kind == 'pptx':
                names = sorted((f.filename for f in infos if re.fullmatch(r'ppt/slides/slide\d+\.xml', f.filename)),
                               key=lambda name: int(re.search(r'slide(\d+)\.xml', name)[1]))
                if len(names) > 200:
                    raise ValueError('Too many slides')
                sections.extend((f'第 {i} 张', xml_text(archive.read(name))) for i, name in enumerate(names, 1))
            elif kind == 'xlsx':
                from openpyxl import load_workbook
                book = load_workbook(io.BytesIO(raw), read_only=True, data_only=True, keep_links=False)
                if len(book.worksheets) > 30:
                    raise ValueError('Too many worksheets')
                for sheet in book.worksheets:
                    if sheet.max_row > 10000 or sheet.max_column > 300 or sheet.max_row * sheet.max_column > 250000:
                        raise ValueError('Worksheet exceeds limits')
                    lines = [' | '.join('' if value is None else str(value) for value in row).rstrip(' |')
                             for row in sheet.iter_rows(values_only=True)]
                    sections.append((sheet.title, '\n'.join(line for line in lines if line)))
                book.close()
            else:
                raise ValueError('Unsupported type')
    text, offsets = '', []
    for label, content in sections:
        text += f'\n[{label}]\n'
        start = len(text)
        text += content.strip() + '\n'
        offsets.append({'label': label, 'start': start, 'end': len(text)})
        if len(text) > MAX_CHARS:
            raise ValueError('Too much text')
    if sum(len(t.strip()) for _, t in sections) < 150:
        raise ValueError('No readable text; image-only documents need manual handling')
    return {'text': text, 'sections': offsets}


if __name__ == '__main__':
    try:
        if sys.platform != 'win32':
            import resource
            resource.setrlimit(resource.RLIMIT_AS, (768*1024*1024, 768*1024*1024))
            resource.setrlimit(resource.RLIMIT_CPU, (35, 35))
        raw = sys.stdin.buffer.read(20*1024*1024 + 1)
        if len(raw) > 20*1024*1024:
            raise ValueError('Input exceeds limit')
        print(json.dumps(extract(raw, sys.argv[1]), ensure_ascii=True))
    except Exception:
        print(json.dumps({'error': '文件无法在限额内提取正文，请核对原件或手动导入可读材料。'}))
        sys.exit(1)
