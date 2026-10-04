from django.contrib import admin

from .models import (MacroDataPoint, MacroIndicator, MacroSourceMapping, MacroImportRun,
                     MacroObservation, MacroObservationRevision)


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(MacroIndicator)
class MacroIndicatorAdmin(ReadOnlyAdmin):
    list_display = ("country", "code", "name", "category", "frequency", "unit", "is_active")
    list_filter = ("country", "category", "frequency", "is_active")
    search_fields = ("code", "name", "source")


@admin.register(MacroDataPoint)
class MacroDataPointAdmin(ReadOnlyAdmin):
    list_display = ("indicator", "period_date", "value", "revised_value", "release_date")
    list_filter = ("indicator__country", "period_date", "release_date")
    search_fields = ("indicator__code", "indicator__name")


@admin.register(MacroSourceMapping)
class MappingAdmin(ReadOnlyAdmin):
    list_display = ("indicator", "provider", "group", "updated_at")
    list_filter = ("provider", "indicator__country")


@admin.register(MacroImportRun)
class RunAdmin(ReadOnlyAdmin):
    list_display = ("group", "status", "started_at", "finished_at")
    list_filter = ("status", "group")


@admin.register(MacroObservation)
class ObservationAdmin(ReadOnlyAdmin):
    list_display = ("mapping", "geography", "period_date", "value", "revision")
    list_filter = ("mapping__indicator__country", "mapping")


@admin.register(MacroObservationRevision)
class RevisionAdmin(ReadOnlyAdmin):
    list_display = ("observation", "number", "value", "observed_at")
