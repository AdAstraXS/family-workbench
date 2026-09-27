import io
import re
import uuid
from urllib.parse import urlsplit, parse_qs
from cryptography.fernet import Fernet
from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q, Sum
from django.http import FileResponse, Http404, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_POST, require_safe

from knowledge.crypto import encrypt_json, _fernet_key
from .ai_enrichment import text_ai_providers, provider_is_configured
from .program_models import ProgramEntry, ProgramRevision, ProgramSettings, ProgramSubscription
from .program_sources import CATALOGUE, ProgramError
from .program_custom_sources import source_spec, inspect_new_source, matches_filters, inspect_bilibili_video
from .program_self_service import ProgramSourceForm, ProgramUploadForm, source_filter_fields, source_form_initial, save_member_upload
from .program_processing import save_revision, month_start, validate_points
from .program_archive import archive_program
from .views import _is_family_admin

STOCK_FILTERS = {
    'MSFT': ('微软', r'微软|(^|[^A-Za-z])(Microsoft|MSFT)([^A-Za-z]|$)'),
    'TSLA': ('特斯拉', r'特斯拉|(^|[^A-Za-z])(Tesla|TSLA)([^A-Za-z]|$)'),
    'SPCX': ('SpaceX', r'(^|[^A-Za-z])(SpaceX|SPCX)([^A-Za-z]|$)'),
    'INTC': ('Intel', r'英特尔|(^|[^A-Za-z])(Intel|INTC)([^A-Za-z]|$)'),
    'NVDA': ('英伟达', r'英伟达|輝達|(^|[^A-Za-z])(Nvidia|NVDA)([^A-Za-z]|$)'),
    'GOOG': ('谷歌', r'谷歌|(^|[^A-Za-z])(Google|Alphabet|GOOGL?)([^A-Za-z]|$)'),
}


class SettingsForm(forms.ModelForm):
    api_key = forms.CharField(label='百炼北京 API Key', required=False, widget=forms.PasswordInput(render_value=False),
                              help_text='留空保留现有 Key；输入后加密保存，不在页面回显。')
    clear_key = forms.BooleanField(label='删除已保存的 Key', required=False)
    class Meta:
        model = ProgramSettings
        fields = ['workspace_id', 'public_base_url', 'allow_asr', 'monthly_asr_cny', 'max_audio_minutes',
                  'summary_provider', 'allow_summary', 'monthly_summary_usd']
        labels = {'workspace_id': '百炼 Workspace ID（可选）', 'public_base_url': '本工作台公网 HTTPS 地址',
            'allow_asr': '允许将已订阅的公开音频发给百炼 Fun-ASR 转写', 'monthly_asr_cny': '每月转写预算（元）',
            'max_audio_minutes': '单集最长分钟数', 'summary_provider': 'AI 整理模型',
            'allow_summary': '允许该模型读取已订阅节目和文章的完整公开正文', 'monthly_summary_usd': '每月摘要预算（美元）'}
        help_texts = {'public_base_url': '供旧转写任务访问限时音频。新任务直接上传至百炼私有临时存储，48 小时后由服务商清理；NAS 音频完成后或 6 小时后删除。',
                      'summary_provider': '沿用现有情报模型的费用上限和密钥配置；这里单独授权完整正文。'}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['summary_provider'].queryset = self.fields['summary_provider'].queryset.filter(
            pk__in=[p.pk for p in text_ai_providers() if provider_is_configured(p)])

    def clean_workspace_id(self):
        value = self.cleaned_data['workspace_id'].strip()
        if value and not re.fullmatch(r'[a-zA-Z0-9-]{1,80}', value):
            raise forms.ValidationError('请输入正确的 Workspace ID。')
        return value

    def clean_public_base_url(self):
        value = self.cleaned_data['public_base_url'].rstrip('/')
        if value:
            parsed = urlsplit(value)
            if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
                raise forms.ValidationError('请输入本工作台的 HTTPS 根地址，不包含路径或凭据。')
        return value

    def clean(self):
        data = super().clean()
        if data.get('allow_asr') and (data.get('clear_key') or not (data.get('api_key') or self.instance.encrypted_credentials)):
            self.add_error('api_key', '开启转写前需要填写 Key。')
        if data.get('allow_summary') and not data.get('summary_provider'):
            self.add_error('summary_provider', '请选择已配置的模型。')
        return data


def family_entries(request):
    member = request.family_member
    if not member:
        raise Http404
    return ProgramEntry.objects.filter(subscription__family=member.family).filter(
        Q(private_owner__isnull=True) | Q(private_owner=member)).select_related('subscription', 'current_revision')


@login_required
def program_list(request):
    entries = family_entries(request)
    source = request.GET.get('source', '')
    subscriptions = {sub.code: sub for sub in ProgramSubscription.objects.filter(family=request.family_member.family)}
    catalogue = {code: spec for code, spec in CATALOGUE.items()}
    catalogue.update({code: source_spec(sub) for code, sub in subscriptions.items()
                      if code not in CATALOGUE and (sub.kind != 'upload' or code == f'upload_{request.family_member.pk}')})
    query = request.GET.get('q', '').strip()[:100]
    stock = request.GET.get('stock', '')
    if stock in STOCK_FILTERS:
        pattern = STOCK_FILTERS[stock][1]
        entries = entries.filter(Q(title__iregex=pattern) | Q(current_revision__text__iregex=pattern))
    if source in catalogue:
        entries = entries.filter(subscription__code=source)
    if query:
        entries = entries.filter(Q(title__icontains=query) | Q(current_revision__text__icontains=query))
    if request.GET.get('state') == 'readable':
        entries = entries.filter(current_revision__isnull=False)
    page = Paginator(entries, 20).get_page(request.GET.get('page'))
    for entry in page:
        entry.source_name = source_spec(entry.subscription)['name']
        entry.summary_preview = ''
        revision = entry.current_revision
        if revision and revision.summary_complete:
            rows = {i: s['text'] for i, s in enumerate(revision.segments, 1)}
            for point in revision.summary.get('points', []):
                try:
                    validate_points({'points': [point]}, set(rows), rows)
                except ProgramError:
                    continue
                entry.summary_preview = point['text']
                break
    return render(request, 'intelligence/program_list.html', {'page': page, 'catalogue': catalogue,
        'source': source, 'query': query, 'stock': stock, 'stocks': STOCK_FILTERS,
        'can_admin': _is_family_admin(request), 'can_write': request.family_member.role != 'viewer'})


@login_required
@sensitive_post_parameters('api_key')
def program_settings(request):
    if not _is_family_admin(request):
        return HttpResponseForbidden('只有家庭管理员可以修改订阅和服务配置。')
    member = request.family_member
    config = ProgramSettings.objects.filter(family=member.family).first() or ProgramSettings(family=member.family)
    if not config.pk and request.is_secure():
        config.public_base_url = request.build_absolute_uri('/').rstrip('/')
    form = SettingsForm(request.POST if request.method == 'POST' and request.POST.get('action') == 'settings' else None, instance=config)
    if request.method == 'POST':
        if request.POST.get('action') == 'subscriptions':
            enabled = set(request.POST.getlist('sources')) & set(CATALOGUE)
            for code in CATALOGUE:
                ProgramSubscription.objects.update_or_create(family=member.family, code=code,
                    defaults={'enabled': code in enabled, 'auto_process': code in request.POST.getlist('auto')})
            messages.success(request, '订阅已保存。后台定时任务将检查更新，首次仅自动处理每个来源最新一篇。')
            return redirect('intelligence:program_settings')
        if form.is_valid():
            try:
                obj = form.save(commit=False)
                if form.cleaned_data['clear_key']:
                    obj.encrypted_credentials = ''
                elif form.cleaned_data['api_key']:
                    obj.encrypted_credentials = encrypt_json({'api_key': form.cleaned_data['api_key'].strip()})
                obj.configured_by = member
                obj.save()
                messages.success(request, '服务配置已保存。' + ('Key 已加密，不会回显。' if obj.encrypted_credentials else '百炼 Key 尚未配置。'))
                return redirect('intelligence:program_settings')
            except Exception:
                form.add_error(None, '配置未保存，请检查服务器加密密钥配置。')
    subscriptions = {s.code: s for s in ProgramSubscription.objects.filter(family=member.family)}
    sources = [{'code': code, **spec, 'subscription': subscriptions.get(code)} for code, spec in CATALOGUE.items()]
    custom_sources = [sub for sub in subscriptions.values() if sub.code not in CATALOGUE and sub.kind != 'upload']
    kind_labels = {'youtube': 'YouTube', 'bilibili': 'B 站', 'podcast': '播客 RSS', 'article': '文章 RSS'}
    for sub in custom_sources:
        sub.kind_label = kind_labels.get(sub.kind, sub.kind)
    used = ProgramEntry.objects.filter(subscription__family=member.family,
        submitted_at__gte=month_start()).aggregate(n=Sum('asr_reserved_cny'))['n'] or 0
    return render(request, 'intelligence/program_settings.html', {'form': form, 'sources': sources,
        'custom_sources': custom_sources,
        'key_configured': bool(config.encrypted_credentials), 'used_asr': used, 'can_admin': True})


@login_required
def program_detail(request, pk):
    entry = get_object_or_404(family_entries(request), pk=pk)
    revision = entry.current_revision
    if request.GET.get('version'):
        revision = get_object_or_404(entry.revisions, pk=request.GET['version'])
    segments = []
    groups = {}
    summary_warning = ''
    if revision:
        for i, segment in enumerate(revision.segments, 1):
            row = {**segment, 'number': i}
            ms = segment.get('start_ms')
            row['timestamp'] = f'{ms // 60000:02d}:{(ms // 1000) % 60:02d}' if ms is not None else ''
            row['url'] = entry.url + '&t=' + str(ms // 1000) if ms is not None and source_spec(entry.subscription)['kind'] == 'youtube' else ''
            segments.append(row)
        for point in revision.summary.get('points', []):
            try:
                rows = {i: s['text'] for i, s in enumerate(revision.segments, 1)}
                validate_points({'points': [point]}, set(rows), rows)
            except ProgramError as exc:
                summary_warning = str(exc)
                continue
            groups.setdefault(point['topic'], []).append(point)
    return render(request, 'intelligence/program_detail.html', {'entry': entry, 'revision': revision,
        'segments': segments, 'groups': groups, 'summary_warning': summary_warning, 'source_name': source_spec(entry.subscription)['name'],
        'versions': entry.revisions.order_by('-pk'), 'can_admin': _is_family_admin(request),
        'can_write': request.family_member.role != 'viewer'})


@login_required
@require_POST
def program_action(request, pk):
    entry = get_object_or_404(family_entries(request), pk=pk)
    action = request.POST.get('action')
    try:
        if action == 'archive':
            revision = get_object_or_404(entry.revisions, pk=request.POST.get('revision_id'))
            document = archive_program(revision, request.family_member)
            messages.success(request, '当前版本的原文和整理结果已保存到知识待整理。')
            return redirect('knowledge:document_detail', pk=document.pk)
        if not _is_family_admin(request):
            return HttpResponseForbidden('只有管理员可以处理订阅任务。')
        with transaction.atomic():
            entry = ProgramEntry.objects.select_for_update().get(pk=entry.pk)
            if entry.lease_until and entry.lease_until > timezone.now():
                raise ProgramError('任务正在运行，请稍后再操作。')
            if action == 'import':
                raw = request.POST.get('transcript', '').strip()
                save_revision(entry, [{'text': p, 'start_ms': None, 'end_ms': None} for p in raw.splitlines() if p.strip()],
                              origin='manual', source_url=entry.url)
                ProgramEntry.objects.filter(pk=entry.pk).update(requested=True)
                messages.success(request, '文字稿已保存为新版本。')
            elif action == 'retry':
                task_id = request.POST.get('task_id', '').strip()
                if task_id:
                    if not re.fullmatch(r'[A-Za-z0-9-]{1,150}', task_id):
                        raise ProgramError('任务 ID 格式错误。')
                    entry.task_id = task_id
                if entry.current_revision_id:
                    revision = entry.current_revision
                    rows = {i: s['text'] for i, s in enumerate(revision.segments, 1)}
                    invalid = []
                    for chunk in revision.chunks.filter(status='success'):
                        try:
                            validate_points(chunk.result, set(rows), rows)
                        except ProgramError:
                            invalid.append(chunk.pk)
                    if invalid:
                        revision.chunks.filter(pk__in=invalid).update(status='pending')
                        revision.summary_complete = False
                        revision.summary = {k: v for k, v in revision.summary.items() if k != 'points'}
                        revision.save(update_fields=['summary', 'summary_complete', 'updated_at'])
                    # Explicit retry retains the cost reservations from previous attempts.
                    entry.current_revision.chunks.filter(status__in=['failed', 'running']).update(status='pending')
                    entry.state = 'ready' if entry.current_revision.summary_complete else 'text_ready'
                elif entry.task_id:
                    entry.state = 'asr_wait'
                elif entry.submitted_at:
                    raise ProgramError('提交结果不确定，请在百炼控制台找到任务 ID 后填写，不能重复提交。')
                else:
                    entry.state = 'new'
                entry.requested, entry.last_error = True, ''
                entry.save()
                messages.success(request, '已加入处理队列，定时任务会继续处理。')
            elif action == 'retry_transfer':
                if entry.current_revision_id or not entry.task_id or entry.asr_error_code != 'FILE_DOWNLOAD_FAILED':
                    raise ProgramError('仅允许恢复已确认的音频下载失败任务，请先查询原任务。')
                entry.retry_audio_transfer, entry.requested, entry.state, entry.last_error = True, True, 'new', ''
                entry.save(update_fields=['retry_audio_transfer', 'requested', 'state', 'last_error', 'updated_at'])
                messages.success(request, '已安排重新传送。系统会再次核对原任务已失败，并保留原任务及全部费用预留。')
            else:
                raise ProgramError('未知操作。')
    except ProgramError as exc:
        messages.error(request, str(exc))
    return redirect('intelligence:program_detail', pk=entry.pk)


@login_required
@require_POST
def program_add_video(request):
    if not _is_family_admin(request):
        return HttpResponseForbidden('只有管理员可以添加节目。')
    parsed = urlsplit(request.POST.get('url', ''))
    video_id = parse_qs(parsed.query).get('v', [''])[0] if parsed.hostname in {'youtube.com', 'www.youtube.com'} else parsed.path.strip('/') if parsed.hostname == 'youtu.be' else ''
    if not re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id):
        messages.error(request, '请输入有效的 YouTube 视频链接。')
        return redirect('intelligence:program_list')
    sub, _ = ProgramSubscription.objects.get_or_create(family=request.family_member.family, code='rhino')
    entry, _ = ProgramEntry.objects.get_or_create(subscription=sub, external_id=video_id,
        defaults={'title': '等待获取节目标题', 'url': 'https://www.youtube.com/watch?v=' + video_id})
    return redirect('intelligence:program_detail', pk=entry.pk)


@login_required
@require_POST
def program_add_bilibili(request):
    if not _is_family_admin(request):
        return HttpResponseForbidden('只有家庭管理员可以添加公开视频。')
    family = request.family_member.family
    try:
        config = ProgramSettings.objects.filter(family=family).first()
        item = inspect_bilibili_video(request.POST.get('url', ''),
                                      config.max_audio_minutes if config else 180)
        sub = ProgramSubscription.objects.filter(family=family, kind='bilibili',
            channel_id=item['channel_id']).first()
        if sub is None:
            sub = ProgramSubscription.objects.create(family=family, code='bili_' + item['channel_id'],
                kind='bilibili', custom_name=item['channel_name'],
                source_url='https://space.bilibili.com/' + item['channel_id'] + '/video',
                channel_id=item['channel_id'], enabled=True, collect_enabled=False, auto_process=False)
        values = {key: item[key] for key in ('url', 'title', 'duration_seconds', 'published_at')}
        entry, _ = ProgramEntry.objects.get_or_create(subscription=sub, external_id=item['external_id'],
            defaults={**values, 'requested': True})
        if not sub.enabled:
            messages.warning(request, '该 UP 主订阅已暂停；请先在订阅设置中恢复。')
        return redirect('intelligence:program_detail', pk=entry.pk)
    except ProgramError as exc:
        messages.error(request, str(exc))
        return redirect('intelligence:program_list')


@login_required
def program_source_manage(request, pk=None):
    if not _is_family_admin(request):
        return HttpResponseForbidden('只有家庭管理员可以管理公开信源。')
    family = request.family_member.family
    source = get_object_or_404(ProgramSubscription, pk=pk, family=family) if pk else None
    if source and (source.code in CATALOGUE or source.kind == 'upload'):
        raise Http404
    manual_only = bool(source and source.kind == 'bilibili' and not source.collect_enabled)
    if request.method == 'POST' and request.POST.get('action') == 'toggle' and source:
        source.enabled = not source.enabled
        source.save(update_fields=['enabled', 'updated_at'])
        messages.success(request, '已暂停订阅。' if not source.enabled else '已恢复订阅。')
        return redirect('intelligence:program_settings')
    if manual_only and request.method == 'POST':
        return HttpResponseForbidden('此 UP 主仅支持逐期添加公开视频。')
    form = ProgramSourceForm(request.POST if request.method == 'POST' else None,
                             initial=source_form_initial(source) if source else None)
    preview = None
    if request.method == 'POST' and form.is_valid():
        values = form.cleaned_data
        if source and (values['kind'] != source.kind or values['url'].strip() != source.source_url):
            form.add_error('url', '已保存来源的身份与地址不能改动；请新增一个订阅。')
        else:
            try:
                preview = inspect_new_source(values['kind'], values['url'],
                    needs_details=bool(values.get('publish_weekday') or values.get('min_duration_minutes')))
                candidate = source or ProgramSubscription(family=family, code='custom', kind=values['kind'])
                for key, value in source_filter_fields(values).items():
                    setattr(candidate, key, value)
                preview['matches'] = [(item, matches_filters(candidate, item)) for item in preview['items'][:12]]
                if request.POST.get('action') == 'save':
                    if source is None:
                        if ProgramSubscription.objects.filter(family=family, kind=values['kind'], source_url=preview['source_url']).exists():
                            raise ProgramError('这个来源已经添加，可从订阅设置中修改筛选条件。')
                        source = ProgramSubscription(family=family, code='custom_' + uuid.uuid4().hex,
                            kind=values['kind'], enabled=True)
                    source.custom_name = values['name'].strip() or preview['name']
                    source.source_url = preview['source_url']
                    source.feed_url = preview['feed_url']
                    source.channel_id = preview['channel_id']
                    source.playlist_id = preview['playlist_id']
                    for key, value in source_filter_fields(values).items():
                        setattr(source, key, value)
                    source.save()
                    messages.success(request, '订阅已保存。定时任务先收集最近内容，再按你的设置处理新节目。')
                    return redirect('intelligence:program_settings')
            except ProgramError as exc:
                form.add_error(None, str(exc))
    return render(request, 'intelligence/program_source_form.html',
                  {'form': form, 'preview': preview, 'source': source, 'manual_only': manual_only})


@login_required
def program_upload(request):
    member = request.family_member
    if not member or member.role == 'viewer':
        return HttpResponseForbidden('当前成员不能上传资料。')
    form = ProgramUploadForm(request.POST if request.method == 'POST' else None,
                             request.FILES if request.method == 'POST' else None)
    if request.method == 'POST' and form.is_valid():
        try:
            config = ProgramSettings.objects.filter(family=member.family).first()
            entry = save_member_upload(member, form.cleaned_data,
                                       max_minutes=config.max_audio_minutes if config else 180)
            messages.success(request, '文件已保存。音视频会按本次授权和家庭预算处理。')
            return redirect('intelligence:program_detail', pk=entry.pk)
        except ProgramError as exc:
            form.add_error(None, str(exc))
    return render(request, 'intelligence/program_upload.html', {'form': form})


@login_required
@require_safe
def program_uploaded_original(request, pk):
    entry = get_object_or_404(family_entries(request), pk=pk, private_owner=request.family_member)
    if not entry.uploaded_original:
        raise Http404
    try:
        with entry.uploaded_original.open('rb') as stored:
            body = Fernet(_fernet_key()).decrypt(stored.read())
    except Exception:
        raise Http404 from None
    response = FileResponse(io.BytesIO(body), as_attachment=True, filename=entry.uploaded_name or 'original.txt',
                            content_type='application/octet-stream')
    response['Cache-Control'] = 'private, no-store'
    response['X-Content-Type-Options'] = 'nosniff'
    return response


@require_safe
def program_audio(request, pk, token):
    # Capability URL only, short lived, bound to the encrypted file and an active ASR job.
    try:
        value = signing.loads(token, salt='program-audio-v1', max_age=21600)
        entry = ProgramEntry.objects.get(pk=pk)
        if value != {'entry': pk, 'file': entry.audio_file.name} or entry.state not in {'asr_submit', 'asr_wait', 'uncertain'}:
            raise ValueError
        if not entry.audio_file or not entry.audio_expires_at or entry.audio_expires_at <= timezone.now():
            raise ValueError
        with entry.audio_file.open('rb') as file:
            body = Fernet(_fernet_key()).decrypt(file.read())
    except Exception:
        raise Http404 from None
    response = FileResponse(io.BytesIO(body), content_type=entry.audio_mime)
    response['Cache-Control'] = 'private, no-store'
    response['Referrer-Policy'] = 'no-referrer'
    response['X-Content-Type-Options'] = 'nosniff'
    return response
