from django.contrib import admin
from django.db.models import Q
from family_core.private_admin import PrivateContentAdmin

from .models import InvestmentNote, InvestmentNoteType


@admin.register(InvestmentNoteType)
class InvestmentNoteTypeAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "sort_order", "is_active", "updated_at")
    list_editable = ("sort_order", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name", "code", "remark")
    ordering = ("sort_order", "id")


@admin.register(InvestmentNote)
class InvestmentNoteAdmin(PrivateContentAdmin):
    def allowed_objects(self, member):
        return InvestmentNote.objects.filter(family=member.family).filter(
            Q(member=member) | Q(visibility=InvestmentNote.VISIBILITY_FAMILY)
        )

    list_display = (
        "title",
        "member",
        "note_type",
        "note_date",
        "visibility",
        "include_in_knowledge",
        "knowledge_state",
        "updated_at",
    )
    list_filter = (
        "family",
        "member",
        "note_type",
        "visibility",
        "include_in_knowledge",
        "knowledge_state",
        "note_date",
    )
    search_fields = ("title", "content", "remark")
