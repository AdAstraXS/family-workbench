from django.urls import path

from . import views


app_name = "intelligence"

urlpatterns = [
    path("people/", views.index, name="index"),
    path("people/events/", views.event_list, name="event_list"),
    path("people/events/new/", views.manual_event_create, name="event_create"),
    path("people/events/<int:pk>/", views.event_detail, name="event_detail"),
    path("people/events/<int:pk>/analyze/", views.event_analyze, name="event_analyze"),
    path("people/events/<int:pk>/edit/", views.event_edit, name="event_edit"),
    path("people/events/<int:pk>/ignore/", views.event_ignore, name="event_ignore"),
    path("people/events/<int:pk>/bookmark/", views.event_toggle_bookmark, name="event_toggle_bookmark"),
    path("people/events/<int:pk>/read/", views.event_mark_read, name="event_mark_read"),
    path("people/events/<int:pk>/archive/", views.event_archive, name="event_archive"),
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
    path("operations/collect/", views.collect_sources_now, name="collect_sources_now"),
]
