from django.contrib import admin

from .models import (
    CollectionRun,
    CollectionRunItem,
    EventAnalysis,
    EventEvidence,
    EventKnowledgeArchive,
    EventMergeRecord,
    EventMergeSuggestion,
    EventSubject,
    EventUserState,
    IntelligenceEvent,
    IntelligenceSource,
    IntelligenceSubject,
    SourceItem,
    SubjectFollow,
    SubjectKnowledgeIdentity,
    SubjectRelation,
)


class SubjectRelationInline(admin.TabularInline):
    model = SubjectRelation
    fk_name = "from_subject"
    extra = 0


@admin.register(IntelligenceSubject)
class IntelligenceSubjectAdmin(admin.ModelAdmin):
    list_display = ("display_name", "canonical_name", "subject_type", "category", "importance_level", "is_active")
    list_filter = ("subject_type", "category", "importance_level", "is_active")
    search_fields = ("display_name", "canonical_name")
    prepopulated_fields = {"slug": ("canonical_name",)}
    inlines = (SubjectRelationInline,)


@admin.register(SubjectRelation)
class SubjectRelationAdmin(admin.ModelAdmin):
    list_display = ("from_subject", "relation_type", "to_subject", "valid_from", "valid_to")
    list_filter = ("relation_type",)
    search_fields = ("from_subject__display_name", "to_subject__display_name")


@admin.register(IntelligenceSource)
class IntelligenceSourceAdmin(admin.ModelAdmin):
    list_display = ("name", "source_type", "source_group", "source_tier", "adapter_key", "last_success_at", "is_active")
    list_filter = ("source_type", "source_group", "source_tier", "adapter_key", "is_active")
    search_fields = ("name", "topics__display_name", "url", "external_id")
    filter_horizontal = ("topics",)


@admin.register(SourceItem)
class SourceItemAdmin(admin.ModelAdmin):
    list_display = ("title", "source", "published_at", "content_depth", "relevance_score", "processing_status", "created_by")
    list_filter = ("processing_status", "content_depth", "source__source_type", "source__source_tier")
    search_fields = ("title", "author_name", "canonical_url", "excerpt")
    readonly_fields = ("content_hash", "fetched_at", "created_at", "updated_at")


class EventSubjectInline(admin.TabularInline):
    model = EventSubject
    extra = 0


class EventEvidenceInline(admin.TabularInline):
    model = EventEvidence
    extra = 0


@admin.register(IntelligenceEvent)
class IntelligenceEventAdmin(admin.ModelAdmin):
    list_display = ("title", "event_type", "occurred_at", "importance_score", "confidence_score", "selection_status", "review_status")
    list_filter = ("event_type", "change_type", "selection_status", "review_status", "occurred_at")
    search_fields = ("title", "summary", "why_it_matters")
    readonly_fields = ("scoring_policy_version", "scoring_breakdown", "cluster_key", "first_seen_at", "last_seen_at", "created_at", "updated_at")
    inlines = (EventSubjectInline, EventEvidenceInline)


@admin.register(EventAnalysis)
class EventAnalysisAdmin(admin.ModelAdmin):
    list_display = (
        "event", "status", "provider", "model_name", "prompt_version",
        "is_current", "tokens_used", "created_at",
    )
    list_filter = ("status", "is_current", "provider", "prompt_version", "created_at")
    search_fields = ("event__title", "model_name", "error_message")
    readonly_fields = (
        "event", "provider", "analysis_request", "model_name", "prompt_version",
        "schema_version", "input_fingerprint", "input_snapshot", "result_json",
        "status", "error_message", "tokens_used", "cost_estimate", "is_current",
        "created_by", "created_at", "updated_at",
    )


@admin.register(EventMergeSuggestion)
class EventMergeSuggestionAdmin(admin.ModelAdmin):
    list_display = (
        "left_event", "right_event", "score", "decision_band", "status",
        "policy_version", "reviewed_by", "reviewed_at",
    )
    list_filter = ("family", "status", "decision_band", "policy_version")
    search_fields = ("left_event__title", "right_event__title")
    readonly_fields = (
        "family", "left_event", "right_event", "recommended_event",
        "recommended_primary_source", "score", "decision_band", "policy_version",
        "reason", "auto_merge_eligible", "requires_individual_review",
        "status", "reviewed_by", "reviewed_at", "created_at", "updated_at",
    )


@admin.register(EventMergeRecord)
class EventMergeRecordAdmin(admin.ModelAdmin):
    list_display = (
        "duplicate_event", "canonical_event", "status", "merged_by", "merged_at",
        "reverted_by", "reverted_at",
    )
    list_filter = ("family", "status", "merged_at")
    search_fields = ("canonical_event__title", "duplicate_event__title")
    readonly_fields = (
        "family", "canonical_event", "duplicate_event", "suggestion", "status",
        "snapshot", "merged_by", "merged_at", "reverted_by", "reverted_at",
        "created_at", "updated_at",
    )


@admin.register(SubjectFollow)
class SubjectFollowAdmin(admin.ModelAdmin):
    list_display = ("family", "subject", "priority", "is_muted", "is_active", "added_by")
    list_filter = ("family", "priority", "is_muted", "is_active")


@admin.register(SubjectKnowledgeIdentity)
class SubjectKnowledgeIdentityAdmin(admin.ModelAdmin):
    list_display = ("author_name", "subject", "family", "is_active", "updated_at")
    list_filter = ("family", "is_active")
    search_fields = ("author_name", "normalized_author_name", "subject__display_name")
    readonly_fields = ("normalized_author_name", "created_at", "updated_at")


@admin.register(EventUserState)
class EventUserStateAdmin(admin.ModelAdmin):
    list_display = ("member", "event", "read_at", "bookmarked_at")
    list_filter = ("member", "read_at", "bookmarked_at")


@admin.register(EventKnowledgeArchive)
class EventKnowledgeArchiveAdmin(admin.ModelAdmin):
    list_display = ("event", "document", "archive_mode", "archived_by", "created_at")
    list_filter = ("archive_mode", "event__family")
    search_fields = ("event__title", "document__title")
    readonly_fields = (
        "event",
        "document",
        "archive_mode",
        "archived_by",
        "last_updated_by",
        "created_at",
        "updated_at",
    )


class CollectionRunItemInline(admin.TabularInline):
    model = CollectionRunItem
    extra = 0
    readonly_fields = (
        "source", "status", "discovered_count", "created_count", "updated_count",
        "ignored_count", "noise_count", "clustered_count", "failed_count",
        "cursor_before", "cursor_after", "error_summary",
    )


@admin.register(CollectionRun)
class CollectionRunAdmin(admin.ModelAdmin):
    list_display = ("run_kind", "family", "status", "started_at", "finished_at", "created_count", "failed_count")
    list_filter = ("run_kind", "status", "started_at")
    readonly_fields = (
        "family", "run_kind", "status", "started_at", "finished_at", "parameters",
        "discovered_count", "created_count", "updated_count", "ignored_count",
        "normalized_count", "classified_count", "noise_count", "clustered_count",
        "selected_count", "review_count", "failed_count", "error_summary", "created_by",
    )
    inlines = (CollectionRunItemInline,)
