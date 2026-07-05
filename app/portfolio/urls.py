from django.urls import path

from . import views


app_name = "portfolio"

urlpatterns = [
    path("", views.overview, name="overview"),
    path("accounts/", views.account_list, name="account_list"),
    path("accounts/prototype/", views.account_prototype, name="account_prototype"),
    path(
        "accounts/prototype/<int:account_id>/",
        views.account_detail_prototype,
        name="account_detail_prototype",
    ),
    path("accounts/create/", views.account_create, name="account_create"),
    path("accounts/<int:pk>/", views.account_detail, name="account_detail"),
    path(
        "accounts/<int:account_id>/cash/create/",
        views.cash_movement_create,
        name="cash_movement_create",
    ),
    path("accounts/<int:pk>/edit/", views.account_edit, name="account_edit"),
    path("securities/", views.security_list, name="security_list"),
    path("securities/watchlist/add/", views.watchlist_add, name="watchlist_add"),
    path("securities/create/", views.security_create, name="security_create"),
    path("securities/<int:pk>/edit/", views.security_edit, name="security_edit"),
    path("positions/", views.position_list, name="position_list"),
    path("positions/create/", views.position_create, name="position_create"),
    path("positions/<int:pk>/edit/", views.position_edit, name="position_edit"),
    path("transactions/", views.transaction_list, name="transaction_list"),
    path(
        "transactions/form-options/",
        views.transaction_form_options,
        name="transaction_form_options",
    ),
    path("transactions/create/", views.transaction_create, name="transaction_create"),
    path("transactions/<int:pk>/edit/", views.transaction_edit, name="transaction_edit"),
]
