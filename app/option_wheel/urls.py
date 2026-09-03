from django.urls import path

from . import close_views, views


app_name = "option_wheel"

urlpatterns = [
    path("", views.index, name="index"),
    path("close/", close_views.index, name="close_index"),
    path("close/refresh/", close_views.refresh, name="close_refresh"),
    path("close/<int:pk>/", close_views.detail, name="close_detail"),
    path("refresh-analysis/", views.refresh_analysis, name="refresh_analysis"),
    path("holdings/", views.holdings, name="holdings"),
    path("underlying/<str:symbol>/", views.underlying_detail, name="underlying_detail"),
    path("decisions/<int:pk>/", views.decision_detail, name="decision_detail"),
]
