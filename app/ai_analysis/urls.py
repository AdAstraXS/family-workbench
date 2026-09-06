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
    path("messages/<int:message_id>/share-preview/", views.answer_share_preview_create, name="answer_share_preview_create"),
    path("shares/<int:share_id>/preview/", views.answer_share_preview, name="answer_share_preview"),
    path("shares/<int:share_id>/publish/", views.answer_share_publish, name="answer_share_publish"),
    path("shares/<int:share_id>/", views.answer_share_detail, name="answer_share_detail"),
    path("shares/<int:share_id>/refresh/", views.answer_share_refresh, name="answer_share_refresh"),
    path("shares/<int:share_id>/withdraw/", views.answer_share_withdraw, name="answer_share_withdraw"),
]
