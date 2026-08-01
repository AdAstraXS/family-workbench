from django.urls import path

from . import views


app_name = "knowledge"

urlpatterns = [
    path("", views.index, name="index"),
    path("inbox/", views.inbox, name="inbox"),
    path("library/", views.library, name="library"),
    path("library/mine/", views.personal_library, name="personal_library"),
    path("library/family/", views.family_library, name="family_library"),
    path("topics/", views.topics, name="topics"),
    path("people/", views.people, name="people"),
    path("architecture/", views.architecture, name="architecture"),
    path("documents/<int:pk>/", views.document_detail, name="document_detail"),
    path(
        "documents/<int:pk>/organize/",
        views.document_organize,
        name="document_organize",
    ),
    path("assets/<int:pk>/download/", views.asset_download, name="asset_download"),
    path(
        "revisions/<int:pk>/raw/",
        views.revision_raw_download,
        name="revision_raw_download",
    ),
    path("sources/", views.sources, name="sources"),
    path("sources/<int:pk>/", views.source_detail, name="source_detail"),
    path("sources/<int:pk>/update/", views.source_update, name="source_update"),
    path("sources/<int:pk>/sync/", views.source_sync, name="source_sync"),
    path(
        "sources/<int:pk>/generate-proposals/",
        views.source_generate_proposals,
        name="source_generate_proposals",
    ),
    path("microsoft/start/", views.microsoft_start, name="microsoft_start"),
    path("microsoft/callback/", views.microsoft_callback, name="microsoft_callback"),
    path(
        "microsoft/notebooks/refresh/",
        views.notebooks_refresh,
        name="notebooks_refresh",
    ),
    path(
        "microsoft/notebooks/select/",
        views.notebook_select,
        name="notebook_select",
    ),
    path(
        "microsoft/disconnect/",
        views.microsoft_disconnect,
        name="microsoft_disconnect",
    ),
    path("jobs/", views.jobs, name="jobs"),
    path("jobs/<int:pk>/", views.job_detail, name="job_detail"),
    path("jobs/<int:pk>/cancel/", views.job_cancel, name="job_cancel"),
    path("jobs/<int:pk>/retry/", views.job_retry, name="job_retry"),
    path("review/", views.review, name="review"),
    path(
        "review/<int:pk>/",
        views.proposal_review,
        name="proposal_review",
    ),
    path(
        "review/bulk/preview/",
        views.proposal_bulk_preview,
        name="proposal_bulk_preview",
    ),
    path(
        "review/bulk/apply/",
        views.proposal_bulk_apply,
        name="proposal_bulk_apply",
    ),
]
