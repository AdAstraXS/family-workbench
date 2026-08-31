from django.urls import path

from . import views


app_name = "option_wheel"

urlpatterns = [
    path("", views.index, name="index"),
]
