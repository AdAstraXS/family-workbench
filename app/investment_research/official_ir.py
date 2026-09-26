"""Issuer-scoped public materials; private dossiers never leave the application."""
import gzip
import hashlib
from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from portfolio.models import Security
from .ir_extraction import EXTRACTOR_VERSION, extract_material, AttachmentTextUnavailable
from .models import (OfficialResearchContentVersion, OfficialResearchDocument,
                     ResearchSourceState, SOURCE_OFFICIAL_IR)
from .providers.ir_http import IRClient, IRError
from .providers.ir_registry import company_for_security
from .providers.official_ir import OfficialIRProvider


def company_security(company):
    # Creating a research target must not create a holding or a quote subscription.
    for market, symbol in company.identities:
        existing = Security.objects.filter(market=market, symbol=symbol, asset_type='stock').first()
        if existing:
            return existing
    return Security.objects.get_or_create(
        market=company.market, symbol=company.symbol,
        defaults={'name': company.name, 'asset_type': 'stock', 'currency': company.currency},
    )[0]


def documents_for_security(security):
    query = Q(security=security)
    company = company_for_security(security)
    if company:
        query |= Q(source__in=[SOURCE_OFFICIAL_IR, 'microsoft_ir'], metadata__ir_company=company.key)
    return OfficialResearchDocument.objects.filter(query)


def sync_official_ir(security, *, client=None, force=False):
    company = company_for_security(security)
    if not company:
        raise IRError('这家公司尚未配置官方 IR 入口。')
    # One state per issuer also serializes requests made through ticker aliases.
    canonical = company_security(company)
    state, _ = ResearchSourceState.objects.get_or_create(security=canonical, source=SOURCE_OFFICIAL_IR,
                                                        defaults={'external_company_id': company.key})
    now = timezone.now()
    with transaction.atomic():
        state = ResearchSourceState.objects.select_for_update().get(pk=state.pk)
        if state.last_checked_at and state.last_checked_at > now - timedelta(minutes=2):
            raise IRError('刚刚检查过官方入口，请两分钟后再试。')
        if not force and state.last_success_at and state.last_success_at > now - timedelta(hours=12):
            return state, 0
        state.last_checked_at = now
        state.save(update_fields=['last_checked_at', 'updated_at'])
    try:
        result = OfficialIRProvider(company, client=client).discover()
        created = 0
        with transaction.atomic():
            for material in result.materials:
                identity = company.key + ':' + hashlib.sha256(material.url.encode()).hexdigest()
                document = OfficialResearchDocument.objects.filter(source=SOURCE_OFFICIAL_IR, external_id=identity).first()
                if document is None and company.key == 'msft':
                    document = OfficialResearchDocument.objects.filter(source='microsoft_ir', source_url=material.url).first()
                if document is None:
                    document, new = OfficialResearchDocument.objects.get_or_create(
                        source=SOURCE_OFFICIAL_IR, external_id=identity,
                        defaults={'security': canonical, 'document_type': material.document_type,
                                  'title': material.title, 'source_url': material.url},
                    )
                    created += int(new)
                document.title = material.title
                document.document_type = material.document_type
                document.published_at = material.published_at or document.published_at
                document.period_end = material.period_end or document.period_end
                document.metadata = {**document.metadata, **material.metadata,
                                     'ir_company': company.key, 'fiscal_year': material.fiscal_year,
                                     'quarter': material.quarter, 'discovered_from': material.discovered_from}
                document.save()
            state.last_success_at = now
            state.last_error = result.directory_error
            state.external_company_id = company.key
            state.cursor = {'periods': result.periods, 'warnings': result.warnings,
                            'urls': [m.url for m in result.materials]}
            state.save()
        return state, created
    except (IRError, ValueError, KeyError, TypeError) as exc:
        message = str(exc) if isinstance(exc, IRError) else '官方目录格式发生变化，请核查来源。'
        ResearchSourceState.objects.filter(pk=state.pk).update(last_error=message[:2000])
        raise IRError(message) from exc


def fetch_ir_content(document, *, client=None):
    try:
        return _fetch_ir_content(document, client=client)
    except IRError as exc:
        with transaction.atomic():
            current = OfficialResearchDocument.objects.select_for_update().get(pk=document.pk)
            current.metadata = {**current.metadata, 'content_error': str(exc)[:500],
                                'content_checked_at': timezone.now().isoformat()}
            current.save(update_fields=['metadata', 'updated_at'])
        raise


def _fetch_ir_content(document, *, client=None):
    company = company_for_security(document.security)
    if document.source not in (SOURCE_OFFICIAL_IR, 'microsoft_ir') or not company:
        raise IRError('这份资料不属于已配置的官方 IR 来源。')
    response = (client or IRClient(company)).get(document.source_url)
    raw_hash = hashlib.sha256(response.raw).hexdigest()
    previous = document.content_versions.first()
    if previous and previous.raw_sha256 == raw_hash and previous.extractor_version == EXTRACTOR_VERSION:
        return previous, False
    extraction_error = ''
    try:
        extracted = extract_material(response)
    except AttachmentTextUnavailable as exc:
        extraction_error = str(exc)
        extracted = {'text': '', 'media_type': exc.media_type, 'sections': [], 'published_at': None}
    text = extracted['text']
    with transaction.atomic():
        document = OfficialResearchDocument.objects.select_for_update().get(pk=document.pk)
        latest = document.content_versions.first()
        if latest and latest.raw_sha256 == raw_hash and latest.extractor_version == EXTRACTOR_VERSION:
            return latest, False
        version = OfficialResearchContentVersion.objects.create(
            document=document, version_number=latest.version_number + 1 if latest else 1,
            source_url=response.url, raw_sha256=raw_hash, raw_gzip=gzip.compress(response.raw, mtime=0),
            content_text=text, content_sha256=hashlib.sha256(text.encode()).hexdigest(),
            extractor_version=EXTRACTOR_VERSION, fetched_at=timezone.now(),
            media_type=extracted['media_type'], sections=extracted['sections'],
        )
        document.content_text = text
        document.content_sha256 = version.content_sha256
        document.fetched_at = version.fetched_at
        document.published_at = document.published_at or extracted['published_at']
        document.metadata = {**document.metadata, 'content_error': extraction_error,
                             'content_checked_at': version.fetched_at.isoformat()}
        document.save()
    return version, True


def ir_coverage(security):
    """Availability is descriptive, not a universal required-metric checklist."""
    company = company_for_security(security)
    if not company:
        return None, [], []
    state = ResearchSourceState.objects.filter(source=SOURCE_OFFICIAL_IR, external_company_id=company.key).order_by('-last_success_at').first()
    docs = documents_for_security(security).filter(metadata__ir_company=company.key)
    if state and state.cursor.get('urls'):
        docs = docs.filter(source_url__in=state.cursor['urls'])
    periods = (state.cursor.get('periods', []) if state else [])
    rows = []
    for year, quarter in periods:
        items = docs.filter(metadata__fiscal_year=year, metadata__quarter=quarter)
        rows.append({'year': year, 'quarter': quarter, 'count': items.count(),
                     'saved': items.exclude(content_text__isnull=True).exclude(content_text='').count(),
                     'types': sorted({d.get_document_type_display() for d in items})})
    return company, rows, state.cursor.get('warnings', []) if state else []
