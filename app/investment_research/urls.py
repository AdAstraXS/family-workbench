from django.urls import path

from . import views

app_name = "investment_research"

urlpatterns = [
    path("", views.index, name="index"),
    path("new/", views.create, name="create"),
    path("explore/new/", views.explore, name="explore"),
    path("<int:pk>/", views.detail, name="detail"),
    path("<int:pk>/first/", views.first_thesis, name="first_thesis"),
    path("<int:pk>/drafts/generate/", views.generate_draft, name="generate_draft"),
    path("<int:pk>/drafts/<int:request_pk>/", views.draft_detail, name="draft_detail"),
    path("<int:pk>/documents/sync/", views.sync_documents, name="sync_documents"),
    path("<int:pk>/edit/", views.edit, name="edit"),
    path("<int:pk>/history/", views.history, name="history"),
    path("<int:pk>/documents/", views.documents, name="documents"),
    path(
        "<int:pk>/documents/<int:document_pk>/",
        views.document_detail,
        name="document_detail",
    ),
    path(
        "<int:pk>/documents/<int:document_pk>/metrics/",
        views.document_metrics,
        name="document_metrics",
    ),
    path(
        "<int:pk>/documents/<int:document_pk>/metrics/fill-history/",
        views.fill_document_metrics_history,
        name="fill_document_metrics_history",
    ),
    path(
        "<int:pk>/documents/<int:document_pk>/fetch/",
        views.fetch_sec_content,
        name="fetch_sec_content",
    ),
    path(
        "<int:pk>/documents/<int:document_pk>/cite/",
        views.create_citation,
        name="create_citation",
    ),
]
