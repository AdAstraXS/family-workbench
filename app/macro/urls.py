from django.urls import path

from . import views


app_name = "macro"

urlpatterns = [
    path("", views.index, name="index"),
    path("sources/", views.status, name="status"),
    path("series/<int:pk>/", views.detail, name="detail"),
    path("observations/<int:pk>/revisions/", views.revisions, name="revisions"),
]
