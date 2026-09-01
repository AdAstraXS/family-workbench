from django.urls import path

from . import views


app_name = "option_wheel"

urlpatterns = [
    path("", views.index, name="index"),
    path("refresh-analysis/", views.refresh_analysis, name="refresh_analysis"),
    path("holdings/", views.holdings, name="holdings"),
    path("underlying/<str:symbol>/", views.underlying_detail, name="underlying_detail"),
    path("decisions/<int:pk>/", views.decision_detail, name="decision_detail"),
]
