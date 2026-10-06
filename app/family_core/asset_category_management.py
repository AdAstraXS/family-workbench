"""Family-scoped dictionary maintenance; never reclassifies financial records."""
import uuid
from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from .asset_classification import PRIMARY_CATEGORIES, SECONDARY_CATEGORIES, categories_for_family, selectable_primary
from .models import AssetCategory, Family, FamilyMember


def category_manager(request):
    member = FamilyMember.objects.filter(user=request.user, is_active=True).select_related('family').first()
    can_manage = bool(member and (member.role == FamilyMember.ROLE_ADMIN or request.user.is_superuser))
    return member, can_manage


def dictionary_context(request):
    member, can_manage = category_manager(request)
    categories = list(categories_for_family(member.family)) if member else []
    roots = sorted((item for item in categories if not item.parent_id), key=lambda item: (
        not item.is_classification_primary, item.display_order, item.name, item.pk,
    ))
    ordered = []
    for root in roots:
        ordered.append(root)
        ordered.extend(sorted((item for item in categories if item.parent_id == root.pk),
            key=lambda item: (item.display_order, item.name, item.pk)))
    return {
        'asset_categories': ordered,
        'can_manage_asset_categories': can_manage,
        'classification_family_id': member.family_id if member else None,
    }


def is_referenced(category):
    for relation in category._meta.related_objects:
        if relation.related_model is AssetCategory:
            continue
        if relation.related_model.objects.filter(**{relation.field.name: category.pk}).exists():
            return True
    return False


def record_dictionary_change(category, user, before):
    metadata = dict(category.extra_data or {})
    metadata['classification_managed'] = True
    changes = list(metadata.get('classification_changes', []))
    changes.append({'at': timezone.now().isoformat(), 'user_id': user.pk, 'before': before,
        'after': {name: getattr(category, name) for name in before}})
    metadata['classification_changes'] = changes
    category.extra_data = metadata


class AssetCategoryManagementForm(forms.ModelForm):
    level = forms.ChoiceField(label='分类层级', choices=[('primary','一级类别'),('secondary','二级类别')])

    class Meta:
        model = AssetCategory
        fields = ['level', 'name', 'parent', 'display_order', 'is_active', 'remark']
        labels = {'parent': '所属一级类别', 'display_order': '显示顺序', 'is_active': '启用'}
        help_texts = {
            'name': '名称修改会同步影响历史记录的显示名称，金额保持不变。',
            'parent': '新增一级类别时留空；已被引用的二级类别不能更换所属一级。',
            'is_active': '停用保留历史记录。停用一级类别后，其下二级类别不再用于新分类选择。',
        }

    def __init__(self, *args, family, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.instance.pk:
            self.instance.family = family
        self.original_parent_id = self.instance.parent_id
        if not self.instance.pk:
            self.instance.code = f'asset-category-{uuid.uuid4().hex[:12]}'
        else:
            self.initial['level'] = 'secondary' if self.instance.parent_id else 'primary'
        parents = selectable_primary(AssetCategory.objects.filter(family=family)).filter(
            Q(is_active=True) | Q(pk=self.original_parent_id),
        ).exclude(pk=self.instance.pk).order_by('display_order','name')
        if 'parent' in self.fields:
            self.fields['parent'].queryset = parents
        for field in self.fields.values():
            field.widget.attrs.setdefault('class','form-control')

    def clean_name(self):
        name = self.cleaned_data['name'].strip()
        if AssetCategory.objects.filter(family=self.instance.family, name=name).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError('当前家庭已有同名类别，请使用不同名称。')
        return name

    def clean(self):
        data = super().clean()
        parent, level = data.get('parent'), data.get('level')
        if level == 'primary' and parent:
            self.add_error('parent','一级类别不能设置所属一级类别。')
        if level == 'secondary' and not parent:
            self.add_error('parent','二级类别必须选择所属一级类别。')
        if parent and data.get('is_active') and not parent.is_active:
            self.add_error('parent','所属一级类别已停用，请先启用一级类别。')
        new_parent_id = parent.pk if parent else None
        if self.instance.pk and new_parent_id != self.original_parent_id:
            system_codes = {code for code, _ in PRIMARY_CATEGORIES} | {code for code, _, _ in SECONDARY_CATEGORIES}
            if is_referenced(self.instance) or self.instance.children.exists() or self.instance.code in system_codes:
                self.add_error('parent','该类别已被引用或属于系统类别，不能改变层级或所属一级；请新建类别。')
        return data


@login_required
def asset_category_edit(request, pk=None):
    member, can_manage = category_manager(request)
    if not can_manage:
        raise PermissionDenied('只有当前家庭管理员可以维护资产类别。')
    instance = get_object_or_404(AssetCategory, pk=pk, family=member.family) if pk else AssetCategory(family=member.family)
    if pk and not instance.parent_id and not instance.is_classification_primary:
        raise PermissionDenied('历史未细分类别仅保留供核对，请新增正式分类。')
    form = AssetCategoryManagementForm(request.POST or None, family=member.family, instance=instance)
    if request.method == 'POST':
        # Dictionary edits and reviewed history batches use the same family lock.
        try:
            with transaction.atomic():
                Family.objects.select_for_update().get(pk=member.family_id)
                if pk:
                    instance = AssetCategory.objects.select_for_update().get(pk=pk, family=member.family)
                before = {name:getattr(instance,name) for name in ('name','parent_id','display_order','is_active')}
                form = AssetCategoryManagementForm(request.POST, family=member.family, instance=instance)
                if form.is_valid():
                    category = form.save(commit=False)
                    record_dictionary_change(category, request.user, before)
                    category.save()
                    messages.success(request,'资产类别已保存，历史金额和分类归属未调整。')
                    return redirect('ledger:category_list')
        except IntegrityError:
            form.add_error(None,'类别名称或代码已存在，请重新检查。')
    return render(request,'form.html',{'form':form,'title':'编辑资产类别' if pk else '新增资产类别',
        'form_intro':'投资组合与家庭账本共用。保存二级类别，通过所属一级类别汇总；停用代替删除。'})
