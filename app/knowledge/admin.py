from django.contrib import admin

from .models import (
    KnowledgeAsset,
    KnowledgeDocument,
    KnowledgeImportBatch,
    KnowledgeImportItem,
    KnowledgeJob,
    KnowledgeJobItem,
    KnowledgeProposal,
    KnowledgeRevision,
    KnowledgeSearchEntry,
    KnowledgeSource,
    SourceConnection,
)


@admin.register(SourceConnection)
class SourceConnectionAdmin(admin.ModelAdmin):
    list_display = (
        "member",
        "provider",
        "account_display_name",
        "status",
        "last_success_at",
    )
    list_filter = ("family", "provider", "status")
    search_fields = ("member__display_name", "account_display_name", "account_email")
    readonly_fields = (
        "encrypted_token_cache",
        "available_notebooks",
        "last_used_at",
        "last_success_at",
        "last_error",
        "created_at",
        "updated_at",
    )

    def get_fields(self, request, obj=None):
        fields = list(super().get_fields(request, obj))
        if "encrypted_token_cache" in fields:
            fields.remove("encrypted_token_cache")
        return fields


@admin.register(KnowledgeSource)
class KnowledgeSourceAdmin(admin.ModelAdmin):
    list_display = ("name", "kind", "owner", "visibility", "status", "last_sync_at")
    list_filter = ("family", "kind", "visibility", "status", "is_enabled")
    search_fields = ("name", "external_id", "key")
    readonly_fields = ("sync_cursor", "last_sync_at", "last_reconciled_at", "last_error")


class KnowledgeRevisionInline(admin.TabularInline):
    model = KnowledgeRevision
    fields = ("revision_number", "content_hash", "source_modified_at", "created_at")
    readonly_fields = fields
    extra = 0
    can_delete = False


@admin.register(KnowledgeDocument)
class KnowledgeDocumentAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "source",
        "owner",
        "visibility",
        "sync_status",
        "knowledge_status",
        "curation_status",
        "content_modified_at",
    )
    list_filter = (
        "family",
        "source__kind",
        "visibility",
        "sync_status",
        "knowledge_status",
        "curation_status",
    )
    search_fields = ("title", "author", "external_id", "confirmed_summary")
    readonly_fields = ("current_revision", "source_deleted_at", "created_at", "updated_at")
    inlines = [KnowledgeRevisionInline]


@admin.register(KnowledgeAsset)
class KnowledgeAssetAdmin(admin.ModelAdmin):
    list_display = ("original_name", "revision", "mime_type", "byte_size", "is_image")
    list_filter = ("mime_type", "is_image")
    search_fields = ("original_name", "external_id", "content_hash")


@admin.register(KnowledgeProposal)
class KnowledgeProposalAdmin(admin.ModelAdmin):
    list_display = (
        "document",
        "proposal_type",
        "status",
        "model_name",
        "confirmed_by",
        "created_at",
    )
    list_filter = ("proposal_type", "status", "model_name")
    search_fields = ("document__title", "content_hash")
    readonly_fields = (
        "document",
        "revision",
        "proposal_type",
        "suggested_value",
        "human_value",
        "model_name",
        "prompt_version",
        "content_hash",
        "confirmed_by",
        "confirmed_at",
        "created_at",
    )


class KnowledgeJobItemInline(admin.TabularInline):
    model = KnowledgeJobItem
    fields = ("external_id", "title", "status", "error_message", "created_at")
    readonly_fields = fields
    extra = 0
    can_delete = False


@admin.register(KnowledgeJob)
class KnowledgeJobAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "job_type",
        "source",
        "status",
        "requested_by",
        "created_at",
        "finished_at",
    )
    list_filter = ("family", "job_type", "status")
    readonly_fields = (
        "requested_by",
        "status",
        "parameters",
        "cursor",
        "total_count",
        "success_count",
        "updated_count",
        "skipped_count",
        "failed_count",
        "started_at",
        "finished_at",
        "heartbeat_at",
        "error_message",
        "result",
        "created_at",
        "updated_at",
    )
    inlines = [KnowledgeJobItemInline]


class KnowledgeImportItemInline(admin.TabularInline):
    model = KnowledgeImportItem
    fields = (
        "relative_path",
        "title",
        "action",
        "status",
        "asset_count",
        "error_message",
    )
    readonly_fields = fields
    extra = 0
    can_delete = False


@admin.register(KnowledgeImportBatch)
class KnowledgeImportBatchAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "source",
        "person_name",
        "requested_by",
        "import_format",
        "status",
        "total_count",
        "error_count",
        "created_at",
    )
    list_filter = ("family", "import_format", "status", "visibility")
    search_fields = ("source_filename", "source_sha256", "source__name", "person_name")
    readonly_fields = (
        "batch_key",
        "source_sha256",
        "total_count",
        "new_count",
        "update_count",
        "skipped_count",
        "duplicate_count",
        "error_count",
        "asset_count",
        "estimated_bytes",
        "previewed_at",
        "confirmed_at",
        "completed_at",
        "rolled_back_at",
        "error_message",
        "result",
        "created_at",
        "updated_at",
    )
    inlines = [KnowledgeImportItemInline]


@admin.register(KnowledgeSearchEntry)
class KnowledgeSearchEntryAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "item_kind",
        "owner",
        "visibility",
        "source_name",
        "knowledge_status",
        "updated_at",
    )
    list_filter = (
        "family",
        "item_kind",
        "visibility",
        "source_kind",
        "knowledge_status",
    )
    search_fields = ("title", "searchable_text")
    readonly_fields = [field.name for field in KnowledgeSearchEntry._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
