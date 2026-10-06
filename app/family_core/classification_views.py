from django import forms
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect, render
from django.urls import reverse
from .models import FamilyMember
from .asset_classification import categories_for_family
from .classification_preview import build_classification_preview

class PreviewForm(forms.Form):
    start = forms.DateField(label='开始日期', widget=forms.DateInput(attrs={'type': 'date'}))
    end = forms.DateField(label='结束日期', widget=forms.DateInput(attrs={'type': 'date'}))

    def clean(self):
        data = super().clean()
        if data.get('start') and data.get('end') and data['start'] > data['end']:
            raise forms.ValidationError('开始日期不能晚于结束日期。')
        return data

@login_required
def classification_preview(request):
    member = FamilyMember.objects.filter(user=request.user, is_active=True).first()
    if not member:
        raise PermissionDenied('需要有效家庭成员身份。')
    url = reverse('admin:family_core_assetcategory_classification_preview')
    return redirect(url + ('?' + request.GET.urlencode() if request.GET else ''))


def admin_classification_preview(request, model_admin):
    if not model_admin.has_view_permission(request):
        raise PermissionDenied('需要资产类别查看权限。')
    member = FamilyMember.objects.filter(user=request.user, is_active=True).select_related('family').first()
    if not member:
        raise PermissionDenied('需要有效家庭成员身份。')
    form = PreviewForm(request.GET or None)
    report = None
    if form.is_bound and form.is_valid():
        report = build_classification_preview(member.family, form.cleaned_data['start'], form.cleaned_data['end'])
    return render(request, 'admin/family_core/assetcategory/classification_preview.html', {
        **model_admin.admin_site.each_context(request),
        'title': '资产分类与历史映射', 'opts': model_admin.model._meta,
        'form': form, 'report': report,
        'categories': categories_for_family(member.family).filter(parent__isnull=False, is_active=True).order_by('parent__display_order', 'display_order'),
    })
