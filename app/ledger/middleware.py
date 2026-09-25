from django.shortcuts import render
from django.utils.deprecation import MiddlewareMixin

from portfolio.valuation import exchange_rate_cache
from .valuation import MissingCashflowRate, cashflow_amount


class FinancialReportMiddleware(MiddlewareMixin):
    def __call__(self, request):
        with exchange_rate_cache():
            return super().__call__(request)

    def process_exception(self, request, exception):
        if isinstance(exception, MissingCashflowRate):
            from family_core.household import get_household_family
            from .models import IncomeRecord, ExpenseRecord
            missing = []
            family = get_household_family()
            for model, kind in [(IncomeRecord, "income"), (ExpenseRecord, "expense")]:
                for record in model.objects.filter(family=family).exclude(currency="CNY"):
                    try:
                        cashflow_amount(record)
                    except MissingCashflowRate as exc:
                        day = record.period_start or getattr(record, f"{kind}_date")
                        missing.append({"message": str(exc), "year": day.year, "pk": record.pk, "kind": kind})
            return render(request, "ledger/missing_rate.html", {"rate_error": str(exception), "missing_rates": missing})
        return None
