from django.contrib import admin
from .models import CompanyIdentity, CompanyMaterial, CompanyMaterialVersion, CompanyAcquisitionJob, ResearchPromptTemplate


class ArchiveAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False
    def has_change_permission(self, request, obj=None):
        return False
    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ResearchPromptTemplate)
class PromptTemplateAdmin(ArchiveAdmin):
    list_display = ('owner', 'dossier', 'revision', 'updated_at')

    def get_queryset(self, request):
        return super().get_queryset(request).filter(owner__user=request.user)


@admin.register(CompanyMaterial)
class MaterialAdmin(ArchiveAdmin):
    list_display = ("security", "title", "kind", "checked_at", "last_error")
    search_fields = ("security__symbol", "title")


@admin.register(CompanyIdentity)
class IdentityAdmin(ArchiveAdmin):
    list_display = ("security", "name", "sec_ticker", "cik")


@admin.register(CompanyMaterialVersion)
class VersionAdmin(ArchiveAdmin):
    list_display = ("material", "number", "report_date", "fetched_at")
    exclude = ("raw_gzip",)


# Job records contain private dossier IDs: inspect through the owner-scoped UI.
