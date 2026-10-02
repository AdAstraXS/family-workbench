from django.urls import path
from . import views

app_name = "investment_watch"
urlpatterns = [
    path(
        "versions/<int:pk>/relation/", views.material_relation, name="material_relation"
    ),
    path("sources/add/", views.source_edit, name="source_add"),
    path("sources/<int:pk>/edit/", views.source_edit, name="source_edit"),
    path("companies/add/", views.company_add, name="company_add"),
    path("companies/<int:pk>/", views.company, name="company"),
    path("", views.news, name="index"),
    path("news/", views.news, name="news"),
    path("topics/", views.topics, name="topics"),
    path("news/<int:pk>/", views.news_detail, name="news_detail"),
    path("news/<int:pk>/associate/", views.news_associate, name="associate"),
    path("items/", views.items, name="items"),
    path("items/<int:pk>/", views.item, name="item"),
    path("items/<int:pk>/request-reading/", views.request_reading, name="request_reading"),
    path("events/<int:pk>/annotation/", views.annotation, name="annotation"),
    path("events/<int:pk>/organize/", views.event_organize, name="event_organize"),
    path("evidence/<int:pk>/review/", views.evidence_review, name="review"),
    path("items/<int:pk>/select/", views.select_research, name="select_research"),
    path("rules/", views.rules, name="rules"),
    path("consent/<int:pk>/", views.consent, name="consent"),
    path("runs/", views.runs, name="runs"),
    path("research-context/", views.research_context, name="research_context"),
    path("saved/", views.saved, name="saved"),
    path("coverage/", views.coverage, name="coverage"),
]
