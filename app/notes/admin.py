from django.contrib import admin

from .models import InvestmentNote


@admin.register(InvestmentNote)
class InvestmentNoteAdmin(admin.ModelAdmin):
    list_display = ("title", "member", "note_type", "visibility", "created_at")
    list_filter = ("family", "member", "note_type", "visibility", "created_at")
    search_fields = ("title", "content", "remark")
