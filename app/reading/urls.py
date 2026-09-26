from django.urls import path

from . import views, views_notes, views_plans, views_artifacts, views_ai


app_name = "reading"

urlpatterns = [
    path("", views.index, name="index"),
    path("upload/", views.upload, name="upload"),
    path("plans/", views_plans.plans, name="plans"),
    path("artifacts/<uuid:artifact_id>/", views_artifacts.detail, name="artifact"),
    path("artifacts/<uuid:artifact_id>/versions/<int:number>/", views_artifacts.detail, name="artifact_version"),
    path("artifacts/<uuid:artifact_id>/versions/<int:number>/file/", views_artifacts.file, name="artifact_file"),
    path("artifacts/<uuid:artifact_id>/versions/<int:number>/archive/", views_artifacts.archive, name="artifact_archive"),
    path("artifacts/<uuid:artifact_id>/edit/", views_artifacts.edit, name="artifact_edit"),
    path("artifacts/<uuid:artifact_id>/share/", views_artifacts.share, name="artifact_share"),
    path("ai/<uuid:job_id>/", views_ai.detail, name="ai_job"),
    path("plans/<int:plan_id>/", views_plans.plan, name="plan"),
    path("plan-items/<int:item_id>/progress/", views_plans.plan_progress, name="plan_progress"),
    path("notes/<uuid:note_id>/", views_notes.note_detail, name="note"),
    path("notes/<uuid:note_id>/comments/", views_notes.comment, name="comment"),
    path("comments/<int:comment_id>/", views_notes.comment_edit, name="comment_edit"),
    path("<uuid:pk>/", views.detail, name="detail"),
    path("<uuid:pk>/edit/", views.edit, name="edit"),
    path("<uuid:pk>/retry/", views.retry, name="retry"),
    path("<uuid:pk>/completion/", views.completion, name="completion"),
    path("<uuid:pk>/read/", views.reader, name="reader"),
    path("<uuid:pk>/manifest/", views.manifest, name="manifest"),
    path("<uuid:pk>/resource/", views.resource, name="resource"),
    path("<uuid:pk>/file/", views.file, name="file"),
    path("<uuid:pk>/position/", views.position, name="position"),
    path("<uuid:pk>/annotations/", views_notes.annotations, name="annotations"),
    path("<uuid:pk>/artifacts/upload/", views_artifacts.upload, name="artifact_upload"),
    path("<uuid:pk>/ai/", views_ai.create, name="ai_create"),
]
