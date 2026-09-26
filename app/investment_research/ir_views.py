"""Explicit IR actions. GETs only read saved content."""
import gzip
import hashlib

from django.contrib import messages
from django.http import Http404, HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render

from .models import OfficialResearchContentVersion, OfficialResearchDocument, ResearchSourceState
from .official_ir import company_security, documents_for_security, fetch_ir_content, sync_official_ir
from .providers.ir_registry import COMPANIES, BY_KEY
from .permissions import get_accessible_dossier_or_404, is_writer
from .providers.ir_http import IRError
from .views import _method, _get_member_or_403, _forbidden


@_method(['GET'])
def catalogue(request):
    member = _get_member_or_403(request)
    if member is None:
        return _forbidden()
    cards = []
    for company in COMPANIES:
        state = ResearchSourceState.objects.filter(source='official_ir', external_company_id=company.key).order_by('-last_success_at').first()
        documents = OfficialResearchDocument.objects.filter(source__in=['official_ir', 'microsoft_ir'], metadata__ir_company=company.key)
        cards.append({'company': company, 'state': state, 'count': documents.count(),
                      'period_count': len(state.cursor.get('periods', [])) if state else 0,
                      'saved': documents.exclude(content_text__isnull=True).exclude(content_text='').count(),
                      'warnings': state.cursor.get('warnings', []) if state else []})
    return render(request, 'investment_research/ir_catalogue.html', {'cards': cards, 'can_write': is_writer(member)})


@_method(['POST'])
def sync_company(request, company_key):
    member = _get_member_or_403(request)
    if member is None:
        return _forbidden()
    if not is_writer(member):
        return HttpResponseForbidden('查看者角色不能同步官方材料。')
    if company_key not in BY_KEY:
        raise Http404
    company = BY_KEY[company_key]
    try:
        state, count = sync_official_ir(company_security(company), force=True)
        if state.last_error:
            messages.warning(request, f'{company.name} 实时目录未完成；已保留可用官方材料，详情见公司卡片。')
        else:
            messages.success(request, f'{company.name} 官方目录已检查，新增 {count} 份材料。')
    except IRError as exc:
        messages.error(request, f'{company.name}：{exc}')
    return redirect('investment_research:ir_catalogue')


@_method(['POST'])
def sync(request, pk):
    member = _get_member_or_403(request)
    if member is None:
        return _forbidden()
    dossier = get_accessible_dossier_or_404(member, pk)
    if not is_writer(member):
        return HttpResponseForbidden('查看者角色不能同步官方材料。')
    try:
        state, created = sync_official_ir(dossier.security, force=True)
        if state.last_error:
            messages.warning(request, f'实时目录未能刷新，保留已核实的官方附件；本次新增 {created} 份。详情见覆盖说明。')
        else:
            messages.success(request, f'官方 IR 目录已检查，新增 {created} 份材料。打开材料即可保存正文和固定引用。')
    except IRError as exc:
        messages.error(request, f'官方 IR 检查未完成：{exc} 已有材料保留。')
    return redirect('investment_research:documents', pk=pk)


@_method(['POST'])
def fetch(request, pk, document_pk):
    member = _get_member_or_403(request)
    if member is None:
        return _forbidden()
    dossier = get_accessible_dossier_or_404(member, pk)
    if not is_writer(member):
        return HttpResponseForbidden('查看者角色不能保存正文。')
    document = get_object_or_404(documents_for_security(dossier.security), pk=document_pk)
    try:
        version, created = fetch_ir_content(document)
        if not version.content_text:
            messages.warning(request, '官方原件已保存，但尚无可引用正文。可下载原件阅读；这份材料暂不提供给 AI。')
        else:
            messages.success(request, f'已保存正文第 {version.version_number} 版。' if created else '正文没有变化，原有固定引用仍有效。')
    except IRError as exc:
        messages.error(request, f'正文未更新：{exc}')
    return redirect('investment_research:document_detail', pk=pk, document_pk=document_pk)


@_method(['GET'])
def original(request, pk, document_pk, version_pk):
    member = _get_member_or_403(request)
    if member is None:
        return _forbidden()
    dossier = get_accessible_dossier_or_404(member, pk)
    document = get_object_or_404(documents_for_security(dossier.security), pk=document_pk)
    version = get_object_or_404(OfficialResearchContentVersion, pk=version_pk, document=document)
    raw = gzip.decompress(bytes(version.raw_gzip))
    if hashlib.sha256(raw).hexdigest() != version.raw_sha256:
        raise Http404('原件校验失败。')
    extensions = {'application/pdf': 'pdf', 'text/html': 'html',
                  'application/vnd.openxmlformats-officedocument.wordprocessingml.document': 'docx',
                  'application/vnd.openxmlformats-officedocument.presentationml.presentation': 'pptx',
                  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': 'xlsx'}
    response = HttpResponse(raw, content_type='application/octet-stream')
    response['Content-Disposition'] = f'attachment; filename="IR-{document.pk}-v{version.version_number}.{extensions.get(version.media_type, "bin")}"'
    response['X-Content-Type-Options'] = 'nosniff'
    response['Content-Security-Policy'] = "sandbox; default-src 'none'"
    return response
