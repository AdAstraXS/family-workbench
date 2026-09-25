from django.contrib import admin
from django.db.models import Q
from family_core.private_admin import PrivateContentAdmin
from .permissions import accessible_artifacts, accessible_documents, accessible_search_entries

from .models import (
    KnowledgeArtifact,
    KnowledgeArtifactEvidence,
    KnowledgeArtifactVersion,
    KnowledgeAsset,
    KnowledgeCategory,
    KnowledgeCurationRevision,
    KnowledgeDocument,
    KnowledgeImportBatch,
    KnowledgeImportItem,
    KnowledgeJob,
    KnowledgeJobItem,
    KnowledgeProposal,
    KnowledgeProposalRun,
    KnowledgeRevision,
    KnowledgeSearchEntry,
    KnowledgeSource,
    KnowledgeTag,
    SourceConnection,
)


class KnowledgeArtifactVersionInline(admin.TabularInline):
    model = KnowledgeArtifactVersion
    fields = (
        "version_number",
        "original_name",
        "content_hash",
        "reference_count",
        "matched_reference_count",
        "created_at",
    )
    readonly_fields = fields
    extra = 0
    can_delete = False


@admin.register(KnowledgeArtifact)
class KnowledgeArtifactAdmin(PrivateContentAdmin):
    def allowed_objects(self, member):
        return accessible_artifacts(member)

    list_display = (
        "title",
        "person_name",
        "artifact_type",
        "owner",
        "visibility",
        "status",
        "updated_at",
    )
    list_filter = ("family", "artifact_type", "visibility", "status")
    search_fields = ("title", "person_name", "description")
    readonly_fields = (
        "normalized_person_name",
        "current_version",
        "confirmed_by",
        "confirmed_at",
        "created_at",
        "updated_at",
    )
    inlines = [KnowledgeArtifactVersionInline]


@admin.register(KnowledgeArtifactEvidence)
class KnowledgeArtifactEvidenceAdmin(PrivateContentAdmin):
    def allowed_objects(self, member):
        return KnowledgeArtifactEvidence.objects.filter(version__artifact__in=accessible_artifacts(member)).filter(Q(document__isnull=True) | Q(document__in=accessible_documents(member)))

    list_display = (
        "citation_title",
        "citation_date",
        "version",
        "status",
        "document",
        "revision",
    )
    list_filter = ("status", "match_method")
    search_fields = ("citation_title", "citation_text", "document__title")
    readonly_fields = (
        "version",
        "reference_key",
        "citation_text",
        "citation_title",
        "citation_date",
        "status",
        "match_method",
        "document",
        "revision",
        "candidate_document_ids",
        "created_at",
    )


@admin.register(KnowledgeCategory)
class KnowledgeCategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "family", "is_active", "merged_into", "updated_at")
    list_filter = ("family", "is_active")
    search_fields = ("name", "normalized_name", "description")
    readonly_fields = ("normalized_name", "aliases", "created_at", "updated_at")


@admin.register(KnowledgeTag)
class KnowledgeTagAdmin(admin.ModelAdmin):
    list_display = ("name", "family", "is_active", "merged_into", "updated_at")
    list_filter = ("family", "is_active")
    search_fields = ("name", "normalized_name", "description")
    readonly_fields = ("normalized_name", "aliases", "created_at", "updated_at")


@admin.register(KnowledgeCurationRevision)
class KnowledgeCurationRevisionAdmin(PrivateContentAdmin):
    def allowed_objects(self, member):
        return KnowledgeCurationRevision.objects.filter(document__in=accessible_documents(member))

    list_display = ("document", "sequence", "change_type", "changed_by", "created_at")
    list_filter = ("change_type",)
    search_fields = ("document__title", "summary", "category")
    readonly_fields = (
        "document",
        "sequence",
        "summary",
        "category",
        "tags",
        "change_type",
        "changed_by",
        "proposal_run",
        "created_at",
    )


@admin.register(SourceConnection)
class SourceConnectionAdmin(PrivateContentAdmin):
    def allowed_objects(self, member):
        return SourceConnection.objects.filter(member=member, family=member.family)

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
class KnowledgeSourceAdmin(PrivateContentAdmin):
    def allowed_objects(self, member):
        return KnowledgeSource.objects.filter(owner=member, family=member.family)

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
class KnowledgeDocumentAdmin(PrivateContentAdmin):
    def allowed_objects(self, member):
        return accessible_documents(member)

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
class KnowledgeAssetAdmin(PrivateContentAdmin):
    def allowed_objects(self, member):
        return KnowledgeAsset.objects.filter(revision__document__in=accessible_documents(member))

    list_display = ("original_name", "revision", "mime_type", "byte_size", "is_image")
    list_filter = ("mime_type", "is_image")
    search_fields = ("original_name", "external_id", "content_hash")


@admin.register(KnowledgeProposal)
class KnowledgeProposalAdmin(PrivateContentAdmin):
    def allowed_objects(self, member):
        return KnowledgeProposal.objects.filter(document__in=accessible_documents(member))

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
        "run",
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


@admin.register(KnowledgeProposalRun)
class KnowledgeProposalRunAdmin(PrivateContentAdmin):
    def allowed_objects(self, member):
        return KnowledgeProposalRun.objects.filter(document__in=accessible_documents(member))

    list_display = (
        "document",
        "sequence",
        "model_name",
        "requested_by",
        "created_at",
    )
    list_filter = ("model_name", "prompt_version")
    search_fields = ("document__title", "content_hash")
    readonly_fields = (
        "document",
        "revision",
        "sequence",
        "requested_by",
        "analysis_request",
        "model_name",
        "prompt_version",
        "content_hash",
        "created_at",
    )


class KnowledgeJobItemInline(admin.TabularInline):
    model = KnowledgeJobItem
    fields = ("external_id", "title", "status", "error_message", "created_at")
    readonly_fields = fields
    extra = 0
    can_delete = False


@admin.register(KnowledgeJob)
class KnowledgeJobAdmin(PrivateContentAdmin):
    def allowed_objects(self, member):
        return KnowledgeJob.objects.filter(family=member.family, source__owner=member)

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
class KnowledgeImportBatchAdmin(PrivateContentAdmin):
    def allowed_objects(self, member):
        return KnowledgeImportBatch.objects.filter(family=member.family, source__owner=member)

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
class KnowledgeSearchEntryAdmin(PrivateContentAdmin):
    def allowed_objects(self, member):
        return accessible_search_entries(member)

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
