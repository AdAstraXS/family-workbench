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

from .models import (ResearchQuestion, ResearchQuestionRevision, ResearchQuestionUpdate,
                     ResearchQuestionAction, ResearchWorkflowSettings, ResearchSupplement)


class PrivateArchiveAdmin(ArchiveAdmin):
    owner_path = 'dossier__owner'

    def get_queryset(self, request):
        return super().get_queryset(request).filter(**{
            self.owner_path + '__user': request.user, self.owner_path + '__is_active': True})


@admin.register(ResearchQuestion)
class QuestionAdmin(PrivateArchiveAdmin):
    list_display = ('dossier', 'title', 'status', 'revision', 'updated_at')


@admin.register(ResearchSupplement)
class SupplementAdmin(PrivateArchiveAdmin):
    list_display = ('dossier', 'title', 'period', 'extraction_note', 'created_at')
    exclude = ('raw_gzip',)


@admin.register(ResearchWorkflowSettings)
class WorkflowSettingsAdmin(PrivateArchiveAdmin):
    owner_path = 'owner'
    list_display = ('owner', 'provider', 'per_call_budget_usd', 'daily_budget_usd', 'revision')


class QuestionRecordAdmin(PrivateArchiveAdmin):
    owner_path = 'question__dossier__owner'


for record in (ResearchQuestionRevision, ResearchQuestionUpdate, ResearchQuestionAction):
    admin.site.register(record, QuestionRecordAdmin)
