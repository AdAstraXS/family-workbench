from django.urls import path

from . import views


app_name = "macro"

urlpatterns = [
    path("", views.index, name="index"),
    path("country/<str:country>/", views.country, name="country"),
    path("indicators/<str:country>/<str:code>/", views.indicator, name="indicator"),
    path("encyclopedia/", views.encyclopedia, name="encyclopedia"),
    path("encyclopedia/<str:country>/<str:code>/", views.guide, name="guide"),
    path("sources/", views.status, name="status"),
    path("series/<int:pk>/", views.detail, name="detail"),
    path("observations/<int:pk>/revisions/", views.revisions, name="revisions"),
]
