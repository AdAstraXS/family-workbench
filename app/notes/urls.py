from django.urls import path

from . import views


app_name = "notes"

urlpatterns = [
    path("", views.index, name="index"),
    path("new/", views.create, name="create"),
    path("<int:pk>/", views.detail, name="detail"),
    path("<int:pk>/edit/", views.edit, name="edit"),
    path(
        "<int:pk>/confirm-knowledge/",
        views.confirm_knowledge,
        name="confirm_knowledge",
    ),
    path("<int:pk>/delete/", views.delete, name="delete"),
]
