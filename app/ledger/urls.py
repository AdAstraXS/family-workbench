from django.urls import path

from . import views


app_name = "ledger"

urlpatterns = [
    path("", views.overview, name="overview"),
    path("annual-budgets/", views.annual_budget_list, name="annual_budget_list"),
    path("annual-budgets/create/", views.annual_budget_create, name="annual_budget_create"),
    path("annual-budgets/<int:pk>/", views.annual_budget_detail, name="annual_budget_detail"),
    path("annual-budgets/<int:pk>/edit/", views.annual_budget_edit, name="annual_budget_edit"),
    path("investment-returns/", views.investment_return_report, name="investment_return_report"),
    path("investment-goals/", views.investment_goal_dashboard, name="investment_goal_dashboard"),
    path("investment-goals/initialize/", views.investment_goal_initialize, name="investment_goal_initialize"),
    path("investment-goals/settings/", views.investment_goal_settings, name="investment_goal_settings"),
    path("investment-goals/actual-override/", views.investment_goal_actual_override, name="investment_goal_actual_override"),
    path("asset-snapshots/", views.asset_snapshot_list, name="asset_snapshot_list"),
    path("asset-snapshots/export/", views.asset_snapshot_export, name="asset_snapshot_export"),
    path("asset-snapshots/create/", views.asset_snapshot_create, name="asset_snapshot_create"),
    path("asset-snapshots/<int:pk>/", views.asset_snapshot_detail, name="asset_snapshot_detail"),
    path("asset-snapshots/<int:pk>/edit/", views.asset_snapshot_edit, name="asset_snapshot_edit"),
    path("accounts/", views.bank_account_list, name="bank_account_list"),
    path("accounts/create/", views.bank_account_create, name="bank_account_create"),
    path("accounts/<int:pk>/edit/", views.bank_account_edit, name="bank_account_edit"),
    path("categories/", views.category_list, name="category_list"),
    path("categories/income/create/", views.income_category_create, name="income_category_create"),
    path("categories/income/<int:pk>/edit/", views.income_category_edit, name="income_category_edit"),
    path("categories/expense/create/", views.expense_category_create, name="expense_category_create"),
    path("categories/expense/<int:pk>/edit/", views.expense_category_edit, name="expense_category_edit"),
    path("income/", views.income_list, name="income_list"),
    path("income/create/", views.income_create, name="income_create"),
    path("income/<int:pk>/edit/", views.income_edit, name="income_edit"),
    path("income/<int:pk>/delete/", views.income_delete, name="income_delete"),
    path("expenses/", views.expense_list, name="expense_list"),
    path("expenses/summary/", views.cashflow_summary, name="cashflow_summary"),
    path("expenses/<int:year>/summary/", views.cashflow_summary, name="cashflow_summary_year"),
    path("expenses/<int:year>/export/", views.expense_year_export, name="expense_year_export"),
    path("expenses/<int:year>/<int:month>/", views.expense_month_detail, name="expense_month_detail"),
    path("expenses/<int:year>/", views.expense_year_detail, name="expense_year_detail"),
    path("expenses/create/", views.expense_create, name="expense_create"),
    path("expenses/import/", views.expense_import, name="expense_import"),
    path("expenses/<int:pk>/edit/", views.expense_edit, name="expense_edit"),
    path("expenses/<int:pk>/delete/", views.expense_delete, name="expense_delete"),
]
