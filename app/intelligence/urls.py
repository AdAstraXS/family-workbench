from django.urls import path

from . import views


app_name = "intelligence"

urlpatterns = [
    path("people/", views.index, name="index"),
    path("people/digests/", views.digest_workbench, name="digest_workbench"),
    path("people/digests/analyze/", views.digest_analyze_batch, name="digest_analyze_batch"),
    path("people/digests/generate/", views.digest_generate, name="digest_generate"),
    path("people/events/", views.event_list, name="event_list"),
    path("people/review/", views.triage_review, name="triage_review"),
    path("people/review/batch/", views.triage_batch_apply, name="triage_batch_apply"),
    path("people/events/new/", views.manual_event_create, name="event_create"),
    path("people/events/<int:pk>/", views.event_detail, name="event_detail"),
    path("people/events/<int:pk>/analyze/", views.event_analyze, name="event_analyze"),
    path("people/events/<int:pk>/edit/", views.event_edit, name="event_edit"),
    path("people/events/<int:pk>/ignore/", views.event_ignore, name="event_ignore"),
    path("people/events/<int:pk>/bookmark/", views.event_toggle_bookmark, name="event_toggle_bookmark"),
    path("people/events/<int:pk>/read/", views.event_mark_read, name="event_mark_read"),
    path("people/events/<int:pk>/archive/", views.event_archive, name="event_archive"),
    path("people/merges/", views.merge_review, name="merge_review"),
    path("people/merges/refresh/", views.merge_suggestion_refresh, name="merge_suggestion_refresh"),
    path("people/merges/batch-accept/", views.merge_suggestion_batch_accept, name="merge_suggestion_batch_accept"),
    path("people/merges/<int:pk>/", views.merge_suggestion_confirm, name="merge_suggestion_confirm"),
    path("people/merges/<int:pk>/reject/", views.merge_suggestion_reject, name="merge_suggestion_reject"),
    path("people/merge-records/<int:pk>/split/", views.merge_record_split, name="merge_record_split"),
    path("people/subjects/", views.subject_list, name="subject_list"),
    path("people/subjects/new/", views.subject_create, name="subject_create"),
    path("people/subjects/<slug:slug>/", views.subject_detail, name="subject_detail"),
    path("people/subjects/<slug:slug>/edit/", views.subject_edit, name="subject_edit"),
    path("people/subjects/<slug:slug>/follow/", views.subject_toggle_follow, name="subject_toggle_follow"),
    path("sources/", views.source_list, name="source_list"),
    path("sources/new/", views.source_create, name="source_create"),
    path("sources/<int:pk>/edit/", views.source_edit, name="source_edit"),
    path("pipeline/", views.pipeline, name="pipeline"),
    path("operations/", views.operations, name="operations"),
    path("operations/run-cycle/", views.run_automatic_cycle_now, name="run_automatic_cycle_now"),
    path("operations/collect/", views.collect_sources_now, name="collect_sources_now"),
]
