from django.contrib import admin

from .models import InvestmentNote, InvestmentNoteType


@admin.register(InvestmentNoteType)
class InvestmentNoteTypeAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "sort_order", "is_active", "updated_at")
    list_editable = ("sort_order", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name", "code", "remark")
    ordering = ("sort_order", "id")


@admin.register(InvestmentNote)
class InvestmentNoteAdmin(admin.ModelAdmin):
    list_display = ("title", "member", "note_type", "note_date", "visibility", "updated_at")
    list_filter = ("family", "member", "note_type", "visibility", "note_date")
    search_fields = ("title", "content", "remark")
