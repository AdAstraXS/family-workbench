from django.urls import path

from . import views


app_name = "portfolio"

urlpatterns = [
    path("", views.overview, name="overview"),
    path(
        "daily-valuation/refresh/",
        views.daily_valuation_refresh,
        name="daily_valuation_refresh",
    ),
    path(
        "reconciliation/",
        views.reconciliation_preview,
        name="reconciliation_preview",
    ),
    path(
        "reconciliation/apply/",
        views.reconciliation_apply,
        name="reconciliation_apply",
    ),
    path(
        "reconciliation/revert/",
        views.reconciliation_revert,
        name="reconciliation_revert",
    ),
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
    path("securities/options/create/", views.option_contract_create, name="option_contract_create"),
    path("securities/bonds/create/", views.bond_create, name="bond_create"),
    path("securities/bonds/<int:pk>/edit/", views.bond_edit, name="bond_edit"),
    path("securities/<int:pk>/edit/", views.security_edit, name="security_edit"),
    path("market-data/", views.market_data_status, name="market_data_status"),
    path("market-data/refresh/", views.market_data_refresh, name="market_data_refresh"),
    path(
        "market-data/<int:pk>/price/",
        views.manual_security_price,
        name="manual_security_price",
    ),
    path(
        "market-data/<int:pk>/config/",
        views.security_quote_config,
        name="security_quote_config",
    ),
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
    path("transactions/<int:pk>/delete/", views.transaction_delete, name="transaction_delete"),
]
