from django.contrib import admin

from .models import HkIpoListing


@admin.register(HkIpoListing)
class HkIpoListingAdmin(admin.ModelAdmin):
    list_display = ("stock_code", "company_name", "listing_date", "status")
    list_filter = ("status", "listing_date")
    search_fields = ("stock_code", "company_name")
