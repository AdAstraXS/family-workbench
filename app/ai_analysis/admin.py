from django.contrib import admin
from family_core.private_admin import PrivateContentAdmin

from .forms import AiModuleModelForm
from .models import AiAnalysisRequest, AiAnalysisResult, AiModuleModel, AiProvider


@admin.register(AiProvider)
class AiProviderAdmin(admin.ModelAdmin):
    list_display = ("name", "provider_type", "model_name", "is_active", "updated_at")
    list_filter = ("provider_type", "is_active")
    search_fields = ("name", "model_name", "base_url")


@admin.register(AiModuleModel)
class AiModuleModelAdmin(admin.ModelAdmin):
    form = AiModuleModelForm
    list_display = ("module", "provider", "provider_model", "updated_at")
    list_select_related = ("provider",)

    @admin.display(description="实际模型")
    def provider_model(self, obj):
        return obj.provider.model_name


@admin.register(AiAnalysisRequest)
class AiAnalysisRequestAdmin(PrivateContentAdmin):
    def allowed_objects(self, member):
        return AiAnalysisRequest.objects.filter(member=member, family=member.family)

    list_display = ("module", "analysis_type", "member", "provider", "status", "created_at")
    list_filter = ("family", "member", "provider", "module", "status", "created_at")
    search_fields = ("prompt", "analysis_type", "error_message")

    def get_queryset(self, request):
        return super().get_queryset(request).exclude(module="investment_research")


@admin.register(AiAnalysisResult)
class AiAnalysisResultAdmin(PrivateContentAdmin):
    def allowed_objects(self, member):
        return AiAnalysisResult.objects.filter(request__member=member, request__family=member.family)

    list_display = ("request", "tokens_used", "cost_estimate", "created_at")
    search_fields = ("result_text",)

    def get_queryset(self, request):
        return super().get_queryset(request).exclude(request__module="investment_research")
