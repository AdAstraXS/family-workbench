from django.contrib import admin
from .models import UsageRecord, BalanceAccount, HostSample, DownloadRecord, CollectorState, RestoreVerification


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self,request): return False
    def has_change_permission(self,request,obj=None): return False
    def has_delete_permission(self,request,obj=None): return False


@admin.register(UsageRecord)
class UsageAdmin(ReadOnlyAdmin):
    list_display=('started_at','vendor','model_name','module','status','total_tokens','audio_seconds','cost_cny')
    list_filter=('vendor','module','status')


@admin.register(BalanceAccount)
class BalanceAdmin(ReadOnlyAdmin):
    exclude=('encrypted_credentials',)
    list_display=('label','status','balance_cny','checked_at')


admin.site.register(HostSample,ReadOnlyAdmin)
admin.site.register(DownloadRecord,ReadOnlyAdmin)
admin.site.register(CollectorState,ReadOnlyAdmin)
admin.site.register(RestoreVerification,ReadOnlyAdmin)
