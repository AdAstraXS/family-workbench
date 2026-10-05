from django import forms
from django.db.models import Q
from django.core.exceptions import ValidationError
from .models import AssetCategory
from .asset_classification import categories_for_family, validate_assignment

class AssetCategorySelect(forms.Select):
    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        option = super().create_option(name, value, label, selected, index, subindex, attrs)
        if value and hasattr(value, 'instance'):
            category = value.instance
            option['attrs']['data-parent-id'] = str(category.parent_id or category.pk)
        return option

class AssetClassificationFormMixin(forms.Form):
    asset_primary = forms.ModelChoiceField(
        label='一级资产类别', queryset=AssetCategory.objects.none(), required=False,
        widget=forms.Select(attrs={'class': 'form-control', 'data-asset-primary': ''}),
    )

    def setup_asset_classification(self, family):
        self.classification_family = family
        categories = categories_for_family(family).filter(is_active=True)
        self.asset_dictionary_ready = categories.filter(parent__isnull=False).exists()
        current = getattr(self.instance, 'asset_category', None)
        if not current:
            current_id = self.data.get(self.add_prefix('asset_category')) or self.initial.get('asset_category')
            if current_id:
                current = categories.filter(pk=current_id).first()
        if current:
            categories = categories_for_family(family).filter(Q(is_active=True) | Q(pk=current.pk))
            self.initial['asset_primary'] = current.parent_id or current.pk
        primary = categories.filter(parent__isnull=True)
        self.fields['asset_primary'].queryset = primary
        if categories.filter(parent__isnull=False).exists():
            secondary = categories.filter(parent__isnull=False)
            if current and not current.parent_id:
                secondary = categories.filter(Q(parent__isnull=False) | Q(pk=current.pk))
            primary = primary.exclude(code='fund') if not (current and current.code == 'fund') else primary
            self.fields['asset_primary'].queryset = primary
        else:
            secondary = categories
        field = self.fields['asset_category']
        field.label = '二级资产类别'
        field.queryset = secondary.order_by('parent__display_order', 'display_order', 'name')
        field.label_from_instance = lambda category: category.name if category.parent_id else f'{category.name}（历史未细分）'
        field.widget = AssetCategorySelect(attrs={'class': 'form-control', 'data-asset-secondary': ''})

    def clean(self):
        cleaned = super().clean()
        category = cleaned.get('asset_category')
        security = cleaned.get('security')
        if not category and security and security.asset_category_id:
            category = cleaned['asset_category'] = security.asset_category
        if not category and getattr(self, 'asset_dictionary_ready', False):
            self.add_error('asset_category', '请选择二级资产类别。')
        primary = cleaned.get('asset_primary')
        if primary and category and (category.parent_id or category.pk) != primary.pk:
            self.add_error('asset_category', '二级资产类别不属于所选一级类别。')
        if primary and not category:
            self.add_error('asset_category', '请选择二级资产类别。')
        if category and not category.parent_id and category.children.filter(is_active=True).exists():
            if getattr(self.instance, 'asset_category_id', None) != category.pk:
                self.add_error('asset_category', '请选择具体二级资产类别。')
        instrument = security.asset_type if security else cleaned.get('asset_type')
        try:
            family = getattr(self, 'classification_family', None)
            if family and not hasattr(family, 'pk'):
                from .models import Family
                family = Family.objects.get(pk=family)
            validate_assignment(category, family=family, instrument=instrument)
        except ValidationError as exc:
            self.add_error('asset_category', exc)
        return cleaned
