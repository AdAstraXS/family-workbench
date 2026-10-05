from django.contrib import admin

from .models import (MacroDataPoint, MacroIndicator, MacroSourceMapping, MacroImportRun,
                     MacroObservation, MacroObservationRevision, MacroMaintenanceRun, MacroOfficialReport, MacroCalendarSnapshot,
                     MacroPublication, MacroAlert, MacroAlertRead, MacroOperationsSnapshot)


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


@admin.register(MacroMaintenanceRun)
class MaintenanceAdmin(ReadOnlyAdmin):
    list_display = ("mode", "status", "started_at", "finished_at")
    list_filter = ("mode", "status")


@admin.register(MacroOfficialReport)
class ReportAdmin(ReadOnlyAdmin):
    list_display = ("title", "group", "period_date", "release_date", "status", "checked_at")
    list_filter = ("group", "status")


@admin.register(MacroCalendarSnapshot)
class CalendarAdmin(ReadOnlyAdmin):
    list_display = ("agency", "source_url", "created_at", "checked_at")


@admin.register(MacroPublication)
class PublicationAdmin(ReadOnlyAdmin):
    list_display = ("agency", "title", "period_date", "release_date", "verified_at")
    list_filter = ("agency",)


@admin.register(MacroAlert)
class AlertAdmin(ReadOnlyAdmin):
    list_display = ("title", "opened_at", "last_seen_at", "resolved_at")


admin.site.register(MacroAlertRead, ReadOnlyAdmin)
admin.site.register(MacroOperationsSnapshot, ReadOnlyAdmin)
