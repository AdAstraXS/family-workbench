from django.contrib import admin

from .models import MacroDataPoint, MacroIndicator


@admin.register(MacroIndicator)
class MacroIndicatorAdmin(admin.ModelAdmin):
    list_display = ("country", "code", "name", "category", "frequency", "unit", "is_active")
    list_filter = ("country", "category", "frequency", "is_active")
    search_fields = ("code", "name", "source")


@admin.register(MacroDataPoint)
class MacroDataPointAdmin(admin.ModelAdmin):
    list_display = ("indicator", "period_date", "value", "revised_value", "release_date")
    list_filter = ("indicator__country", "period_date", "release_date")
    search_fields = ("indicator__code", "indicator__name")
