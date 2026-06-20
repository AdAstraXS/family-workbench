from django.contrib import admin

from .models import AiAnalysisRequest, AiAnalysisResult, AiProvider


@admin.register(AiProvider)
class AiProviderAdmin(admin.ModelAdmin):
    list_display = ("name", "provider_type", "model_name", "is_active", "updated_at")
    list_filter = ("provider_type", "is_active")
    search_fields = ("name", "model_name", "base_url")


@admin.register(AiAnalysisRequest)
class AiAnalysisRequestAdmin(admin.ModelAdmin):
    list_display = ("module", "analysis_type", "member", "provider", "status", "created_at")
    list_filter = ("family", "member", "provider", "module", "status", "created_at")
    search_fields = ("prompt", "analysis_type", "error_message")


@admin.register(AiAnalysisResult)
class AiAnalysisResultAdmin(admin.ModelAdmin):
    list_display = ("request", "tokens_used", "cost_estimate", "created_at")
    search_fields = ("result_text",)
