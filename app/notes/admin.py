from django.contrib import admin

from .models import InvestmentNote


@admin.register(InvestmentNote)
class InvestmentNoteAdmin(admin.ModelAdmin):
    list_display = ("title", "member", "note_type", "note_date", "visibility", "updated_at")
    list_filter = ("family", "member", "note_type", "visibility", "note_date")
    search_fields = ("title", "content", "remark")
