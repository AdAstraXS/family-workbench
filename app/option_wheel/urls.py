from django.urls import path

from . import advice_views, close_views, job_views, position_views, screen_views, views


app_name = "option_wheel"

urlpatterns = [
    path("", screen_views.index, name="index"),
    path("watch/", screen_views.watch_action, name="watch_action"),
    path("analyze/", screen_views.analyze, name="analyze"),
    path("close/", close_views.index, name="close_index"),
    path("close/refresh/", close_views.refresh, name="close_refresh"),
    path("close/<int:pk>/", close_views.detail, name="close_detail"),
    path("refresh-analysis/", views.refresh_analysis, name="refresh_analysis"),
    path("jobs/<uuid:pk>/", job_views.detail, name="job_detail"),
    path("jobs/<uuid:pk>/status/", job_views.status, name="job_status"),
    path("holdings/", views.holdings, name="holdings"),
    path("holdings/positions/<int:pk>/", position_views.position_detail, name="position_detail"),
    path("holdings/positions/<int:pk>/scan/", position_views.scan_position, name="scan_position"),
    path("holdings/review/", position_views.record_put_review, name="record_put_review"),
    path("holdings/review/<int:pk>/link/", position_views.link_put_transaction, name="link_put_transaction"),
    path("holdings/quotes/refresh/", position_views.refresh_put_quotes, name="refresh_put_quotes"),
    path("underlying/<str:symbol>/", views.underlying_detail, name="underlying_detail"),
    path("decisions/<int:pk>/", views.decision_detail, name="decision_detail"),
    path("decisions/<int:pk>/advice/", advice_views.preview, name="advice_preview"),
    path("decisions/<int:pk>/advice/generate/", advice_views.generate, name="advice_generate"),
]
