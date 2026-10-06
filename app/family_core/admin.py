from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.urls import path

from .asset_category_management import AssetCategoryManagementForm, category_manager, record_dictionary_change
from .asset_classification import categories_for_family

from .models import AccountRegion, AccountType, AssetCategory, AssetClassificationAudit, Currency, ExchangeRate, Family, FamilyMember, SiteSetting


@admin.register(Family)
class FamilyAdmin(admin.ModelAdmin):
    list_display = ("name", "base_currency", "created_at", "updated_at")
    search_fields = ("name", "remark")

    def has_add_permission(self, request):
        return not Family.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(SiteSetting)
class SiteSettingAdmin(admin.ModelAdmin):
    list_display = ("household_name", "base_currency", "timezone", "updated_at")

    def has_add_permission(self, request):
        return not SiteSetting.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(FamilyMember)
class FamilyMemberAdmin(admin.ModelAdmin):
    list_display = ("display_name", "display_order", "family", "role", "is_active", "created_at")
    list_filter = ("family", "role", "is_active")
    search_fields = ("display_name", "remark")


@admin.register(Currency)
class CurrencyAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "symbol", "is_active")
    list_filter = ("is_active",)
    search_fields = ("code", "name")


@admin.register(ExchangeRate)
class ExchangeRateAdmin(admin.ModelAdmin):
    list_display = ("base_currency", "quote_currency", "rate", "rate_date", "source")
    list_filter = ("base_currency", "quote_currency", "rate_date")
    search_fields = ("source",)


@admin.register(AccountType)
class AccountTypeAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "family", "display_order", "is_active")
    list_filter = ("family", "is_active")
    search_fields = ("name", "code", "remark")


@admin.register(AssetCategory)
class AssetCategoryAdmin(admin.ModelAdmin):
    form = AssetCategoryManagementForm
    fields = ('level', 'name', 'parent', 'display_order', 'is_active', 'remark', 'code', 'family')
    readonly_fields = ('code', 'family')
    change_list_template = 'admin/family_core/assetcategory/change_list.html'
    list_display = ("name", "parent", "code", "family", "display_order", "is_active")
    list_filter = (("parent", admin.RelatedOnlyFieldListFilter), "is_active")
    search_fields = ("name", "code", "remark")

    def get_queryset(self, request):
        member, _ = category_manager(request)
        return categories_for_family(member.family) if member else super().get_queryset(request).none()

    def has_view_permission(self, request, obj=None):
        member, _ = category_manager(request)
        return bool(member and super().has_view_permission(request, obj))

    def has_add_permission(self, request):
        _, can_manage = category_manager(request)
        return can_manage and super().has_add_permission(request)

    def has_change_permission(self, request, obj=None):
        member, can_manage = category_manager(request)
        editable = bool(member and (obj is None or (obj.family_id == member.family_id and (
            obj.parent_id or obj.is_classification_primary))))
        return bool(can_manage and editable and super().has_change_permission(request, obj))

    def get_form(self, request, obj=None, **kwargs):
        member, _ = category_manager(request)
        if not member:
            raise PermissionDenied('需要有效家庭成员身份。')
        form_class = super().get_form(request, obj, **kwargs)

        class ScopedForm(form_class):
            def __init__(self, *args, **form_kwargs):
                form_kwargs['family'] = member.family
                super().__init__(*args, **form_kwargs)

        return ScopedForm

    def changeform_view(self, request, object_id=None, form_url='', extra_context=None):
        if request.method == 'POST':
            member, can_manage = category_manager(request)
            if not can_manage:
                raise PermissionDenied('只有当前家庭管理员可以维护资产类别。')
            with transaction.atomic():
                Family.objects.select_for_update().get(pk=member.family_id)
                return super().changeform_view(request, object_id, form_url, extra_context)
        return super().changeform_view(request, object_id, form_url, extra_context)

    def save_model(self, request, obj, form, change):
        names = ('name', 'parent_id', 'display_order', 'is_active')
        old = AssetCategory.objects.get(pk=obj.pk) if change else AssetCategory()
        record_dictionary_change(obj, request.user, {name: getattr(old, name) for name in names})
        super().save_model(request, obj, form, change)

    def get_urls(self):
        from .classification_views import admin_classification_preview
        return [path('classification-preview/', self.admin_site.admin_view(
            lambda request: admin_classification_preview(request, self)),
            name='family_core_assetcategory_classification_preview')] + super().get_urls()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AssetClassificationAudit)
class AssetClassificationAuditAdmin(admin.ModelAdmin):
    list_display = ("batch_id", "family", "model_label", "object_id", "created_at")
    readonly_fields = tuple(field.name for field in AssetClassificationAudit._meta.fields)
    list_filter = ("family", "model_label")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AccountRegion)
class AccountRegionAdmin(admin.ModelAdmin):
    list_display = ("name", "family", "display_order", "is_active")
    list_filter = ("family", "is_active")
    search_fields = ("name", "remark")
