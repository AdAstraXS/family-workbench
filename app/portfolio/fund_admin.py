from django.contrib import admin
from family_core.permissions import current_member
from .fund_models import (FundReference, FundDcaPlan, FundDcaChange, FundNavRecord,
                          FundDisclosureSnapshot, FundRefreshRun)


class FundReadOnlyAdmin(admin.ModelAdmin):
    def get_readonly_fields(self, request, obj=None):
        return tuple(field.name for field in self.model._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        if request.user.is_superuser:
            return queryset
        member = current_member(request)
        if member is None:
            return queryset.none()
        if self.model in [FundDcaPlan, FundRefreshRun]:
            return queryset.filter(family=member.family)
        if self.model is FundDcaChange:
            return queryset.filter(plan__family=member.family)
        return queryset  # Public disclosures have no household financial fields.


@admin.register(FundDcaPlan)
class FundDcaPlanAdmin(FundReadOnlyAdmin):
    list_display = ["name", "member", "fund", "frequency", "amount", "fee_rate", "balance", "balance_date"]
    search_fields = ["name", "fund__code", "fund__name"]


@admin.register(FundReference)
class FundReferenceAdmin(FundReadOnlyAdmin):
    list_display = ["code", "name", "latest_nav", "nav_date", "last_attempt_at"]
    search_fields = ["code", "name"]


for model in [FundDcaChange, FundNavRecord, FundDisclosureSnapshot, FundRefreshRun]:
    admin.site.register(model, FundReadOnlyAdmin)
