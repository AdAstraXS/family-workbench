from django.contrib import admin

from .permissions import current_member


class PrivateContentAdmin(admin.ModelAdmin):
    """Read-only inspection within business permissions, including superusers.

    Content mutations belong to application services. Read-only fields also
    remove relation selectors that could disclose another member's content.
    """

    def allowed_objects(self, member):
        raise NotImplementedError

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        member = current_member(request)
        if member is None:
            return queryset.none()
        return queryset.filter(pk__in=self.allowed_objects(member).values("pk"))

    def get_list_filter(self, request):
        return ()

    def get_readonly_fields(self, request, obj=None):
        return tuple(field.name for field in self.model._meta.fields)

    def has_view_permission(self, request, obj=None):
        return bool(
            current_member(request)
            and super().has_view_permission(request, obj)
            and (obj is None or self.get_queryset(request).filter(pk=obj.pk).exists())
        )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
