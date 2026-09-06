from django.urls import path

from . import views


app_name = "ai_analysis"

urlpatterns = [
    path("", views.index, name="index"),
    path("conversations/new/", views.conversation_create, name="conversation_create"),
    path("conversations/<int:conversation_id>/", views.conversation_detail, name="conversation"),
    path("conversations/<int:conversation_id>/archive/", views.conversation_archive, name="conversation_archive"),
    path("memories/new/", views.memory_create, name="memory_create"),
    path("memories/<int:memory_id>/confirm/", views.memory_confirm, name="memory_confirm"),
    path("memories/<int:memory_id>/revise/", views.memory_revise, name="memory_revise"),
    path("memories/<int:memory_id>/delete/", views.memory_delete, name="memory_delete"),
]
