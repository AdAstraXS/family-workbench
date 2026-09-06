from django.contrib import admin

from .models import (
    AiAnalysisRequest,
    AiAnalysisResult,
    AiAnswerShare,
    AiConversation,
    AiConversationMessage,
    AiMemory,
    AiOutboundAuthorization,
    AiProvider,
)


@admin.register(AiAnswerShare)
class AiAnswerShareAdmin(admin.ModelAdmin):
    list_display = ("title", "owner", "status", "source_message", "published_at", "created_at")
    list_filter = ("family", "status", "published_at")
    search_fields = ("title", "answer_text_snapshot", "owner__display_name")
    readonly_fields = (
        "family", "owner", "source_message", "answer_text_snapshot",
        "evidence_snapshot", "source_message_hash", "published_at",
        "paused_at", "pause_reason", "withdrawn_at", "created_at", "updated_at",
    )


@admin.register(AiProvider)
class AiProviderAdmin(admin.ModelAdmin):
    list_display = (
        "name", "provider_type", "model_name", "execution_location", "is_active", "updated_at"
    )
    list_filter = ("provider_type", "execution_location", "is_active")
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


class AiConversationMessageInline(admin.TabularInline):
    model = AiConversationMessage
    extra = 0
    readonly_fields = ("sequence", "role", "content", "data_types", "evidence_refs", "created_at")


@admin.register(AiConversation)
class AiConversationAdmin(admin.ModelAdmin):
    list_display = ("title", "member", "financial_scope", "is_archived", "updated_at")
    list_filter = ("family", "member", "financial_scope", "is_archived")
    search_fields = ("title",)
    inlines = (AiConversationMessageInline,)


@admin.register(AiMemory)
class AiMemoryAdmin(admin.ModelAdmin):
    list_display = ("id", "visibility", "owner", "status", "version", "created_by", "updated_at")
    list_filter = ("family", "visibility", "status")
    search_fields = ("content", "source_note")


@admin.register(AiOutboundAuthorization)
class AiOutboundAuthorizationAdmin(admin.ModelAdmin):
    list_display = ("member", "provider", "data_type", "is_allowed", "updated_at")
    list_filter = ("family", "member", "provider", "data_type", "is_allowed")
