from django.urls import path

from . import views

app_name = "investment_research"

urlpatterns = [
    path("", views.index, name="index"),
    path("new/", views.create, name="create"),
    path("<int:pk>/", views.detail, name="detail"),
    path("<int:pk>/edit/", views.edit, name="edit"),
    path("<int:pk>/history/", views.history, name="history"),
    path("<int:pk>/documents/", views.documents, name="documents"),
    path(
        "<int:pk>/documents/<int:document_pk>/",
        views.document_detail,
        name="document_detail",
    ),
]
