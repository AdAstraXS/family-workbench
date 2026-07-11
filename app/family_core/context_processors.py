from django.urls import reverse

from .models import SiteSetting


def page_navigation(request):
    match = getattr(request, "resolver_match", None)
    if not match:
        return {}
    app_name = match.app_name
    url_name = match.url_name
    kwargs = match.kwargs

    if app_name == "dashboard":
        return {}

    if app_name == "ledger":
        if url_name == "overview":
            parent_url = reverse("dashboard:home")
        elif url_name in {"expense_month_detail", "expense_year_detail"}:
            parent_url = reverse(
                "ledger:cashflow_summary_year",
                kwargs={"year": kwargs["year"]},
            )
        elif url_name == "cashflow_summary_year":
            parent_url = reverse("ledger:expense_list")
        elif url_name == "cashflow_summary":
            parent_url = reverse("ledger:expense_list")
        elif url_name in {"asset_snapshot_detail"}:
            parent_url = reverse("ledger:asset_snapshot_list")
        elif url_name == "asset_snapshot_edit":
            parent_url = reverse(
                "ledger:asset_snapshot_detail",
                kwargs={"pk": kwargs["pk"]},
            )
        elif url_name in {"asset_snapshot_create", "asset_snapshot_export"}:
            parent_url = reverse("ledger:asset_snapshot_list")
        elif url_name == "annual_budget_detail":
            parent_url = reverse("ledger:annual_budget_list")
        elif url_name == "annual_budget_edit":
            parent_url = reverse(
                "ledger:annual_budget_detail",
                kwargs={"pk": kwargs["pk"]},
            )
        elif url_name == "annual_budget_create":
            parent_url = reverse("ledger:annual_budget_list")
        elif url_name in {"bank_account_create", "bank_account_edit"}:
            parent_url = reverse("ledger:bank_account_list")
        elif url_name in {
            "income_category_create",
            "income_category_edit",
            "expense_category_create",
            "expense_category_edit",
        }:
            parent_url = reverse("ledger:category_list")
        elif url_name in {"income_create", "income_edit", "income_delete"}:
            parent_url = reverse("ledger:income_list")
        elif url_name in {
            "expense_create",
            "expense_edit",
            "expense_delete",
            "expense_import",
        }:
            parent_url = reverse("ledger:expense_list")
        else:
            parent_url = reverse("ledger:overview")
        return {"page_parent_url": parent_url}

    if app_name == "ipo":
        if url_name == "index":
            parent_url = reverse("dashboard:home")
        elif url_name in {
            "listing_detail",
            "listing_create",
            "recognize_listing_image",
        }:
            parent_url = reverse("ipo:listing_list")
        elif url_name == "listing_edit":
            parent_url = reverse(
                "ipo:listing_detail",
                kwargs={"pk": kwargs["pk"]},
            )
        elif url_name in {
            "subscription_trade_create",
            "subscription_trade_edit",
            "subscription_trade_delete",
        }:
            parent_url = reverse("ipo:subscription_trade_list")
        else:
            parent_url = reverse("ipo:index")
        return {"page_parent_url": parent_url}

    if app_name == "portfolio":
        if url_name == "overview":
            parent_url = reverse("dashboard:home")
        elif url_name == "cash_movement_create":
            parent_url = reverse(
                "portfolio:account_detail",
                kwargs={"pk": kwargs["account_id"]},
            )
        elif url_name == "option_contract_create":
            parent_url = reverse("portfolio:security_list")
        elif url_name.endswith("_create") or url_name.endswith("_edit"):
            parent_url = reverse(
                f"portfolio:{url_name.rsplit('_', 1)[0]}_list"
            )
        else:
            parent_url = reverse("portfolio:overview")
        return {"page_parent_url": parent_url}

    if app_name == "notes":
        if url_name == "index":
            parent_url = reverse("dashboard:home")
        elif url_name in {"edit", "delete"}:
            parent_url = reverse("notes:detail", kwargs={"pk": kwargs["pk"]})
        else:
            parent_url = reverse("notes:index")
        return {"page_parent_url": parent_url}

    return {"page_parent_url": reverse("dashboard:home")}


def site_identity(request):
    setting = SiteSetting.objects.filter(pk=1).first()
    return {
        "site_household_name": setting.household_name if setting else "家庭工作台",
        "site_base_currency": setting.base_currency if setting else "CNY",
    }
