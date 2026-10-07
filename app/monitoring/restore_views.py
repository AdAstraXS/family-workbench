from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods
from family_core.permissions import current_member
from .models import RestoreVerification
from .restore_reports import CHECKS, validate_report


@login_required
@require_http_methods(['GET', 'POST'])
def restore_verifications(request):
    member = current_member(request)
    if member is None:
        raise PermissionDenied
    error = ''
    if request.method == 'POST':
        if member.role != 'admin':
            raise PermissionDenied
        upload = request.FILES.get('report')
        try:
            if upload is None or upload.size > 65536:
                raise ValueError('请选择不超过 64 KiB 的校验报告。')
            data = validate_report(upload.read(65537))
            _, created = RestoreVerification.objects.get_or_create(
                family=member.family, report_sha256=data.pop('report_sha256'), defaults={**data, 'recorded_by': member})
            messages.success(request, '恢复校验结果已登记。' if created else '此份校验报告已登记。')
            return redirect('monitoring:restore_verifications')
        except ValueError as exc:
            error = str(exc)
    records = list(RestoreVerification.objects.filter(family=member.family).select_related('recorded_by')[:20])
    for record in records:
        record.check_rows = [{**row, 'label': CHECKS[row['key']],
                              'status_label': {'passed': '通过', 'failed': '失败', 'unverified': '未验证'}[row['status']]}
                             for row in record.checks]
    return render(request, 'monitoring/restore.html', {'records': records, 'error': error, 'can_record': member.role == 'admin'})
