from datetime import datetime, time

from django.db import migrations
from django.utils import timezone


def index_existing_notes(apps, schema_editor):
    Family = apps.get_model("family_core", "Family")
    KnowledgeSource = apps.get_model("knowledge", "KnowledgeSource")
    KnowledgeSearchEntry = apps.get_model("knowledge", "KnowledgeSearchEntry")
    InvestmentNote = apps.get_model("notes", "InvestmentNote")

    for family in Family.objects.all().iterator():
        KnowledgeSource.objects.get_or_create(
            family=family,
            key="internal:investment-notes",
            defaults={
                "kind": "internal_notes",
                "name": "随手记",
                "visibility": "family",
                "status": "active",
                "is_enabled": True,
            },
        )

    for note in InvestmentNote.objects.select_related("member", "note_type").iterator():
        tags = note.tags or []
        tags_text = "\n".join(str(tag) for tag in tags)
        searchable_text = "\n".join(
            [
                note.title or "",
                note.content or "",
                note.ai_summary or "",
                note.note_type.name if note.note_type_id else "",
                tags_text,
                note.member.display_name,
            ]
        ).casefold()
        content_time = timezone.make_aware(
            datetime.combine(note.note_date, time.min),
            timezone.get_current_timezone(),
        )
        KnowledgeSearchEntry.objects.update_or_create(
            family=note.family,
            item_kind="investment_note",
            object_id=str(note.pk),
            defaults={
                "owner": note.member,
                "visibility": note.visibility,
                "title": note.title,
                "body": note.content,
                "summary": note.ai_summary,
                "source_kind": "internal_notes",
                "source_name": "随手记",
                "author_name": note.member.display_name,
                "category": note.note_type.name if note.note_type_id else "",
                "tags": tags,
                "tags_text": tags_text,
                "searchable_text": searchable_text,
                "curation_status": "confirmed",
                "content_time": content_time,
            },
        )


def remove_note_projection(apps, schema_editor):
    KnowledgeSearchEntry = apps.get_model("knowledge", "KnowledgeSearchEntry")
    KnowledgeSource = apps.get_model("knowledge", "KnowledgeSource")
    KnowledgeSearchEntry.objects.filter(item_kind="investment_note").delete()
    KnowledgeSource.objects.filter(key="internal:investment-notes").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("knowledge", "0001_initial"),
        ("notes", "0003_investmentnotetype"),
    ]

    operations = [
        migrations.RunPython(index_existing_notes, remove_note_projection),
    ]
