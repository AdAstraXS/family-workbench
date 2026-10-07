from django.urls import path

from . import views, review_views


app_name = "family_core"

urlpatterns = [
    path('review/', review_views.review, name='review'),
    path('search/', review_views.search, name='search'),
    path('financial-basis/', review_views.basis, name='financial_basis'),
    path('changes/', review_views.changes, name='changes'),
    path("members/", views.member_list, name="member_list"),
]
