from django.urls import path

from . import views


app_name = "family_core"

urlpatterns = [
    path("members/", views.member_list, name="member_list"),
]
