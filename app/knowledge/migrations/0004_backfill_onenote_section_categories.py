from django.db import migrations


def backfill_onenote_section_categories(apps, schema_editor):
    KnowledgeDocument = apps.get_model("knowledge", "KnowledgeDocument")
    KnowledgeSearchEntry = apps.get_model("knowledge", "KnowledgeSearchEntry")

    documents = (
        KnowledgeDocument.objects.filter(
            source__kind="onenote",
        )
        .exclude(section_name="")
        .only("id", "section_name", "hierarchy", "category")
    )
    for document in documents.iterator():
        category = document.category or document.section_name[:100]
        if not document.category:
            KnowledgeDocument.objects.filter(pk=document.pk, category="").update(
                category=category
            )
        entry = KnowledgeSearchEntry.objects.filter(document_id=document.pk).first()
        if entry is None:
            continue
        searchable_text = entry.searchable_text or ""
        hierarchy = document.hierarchy or {}
        source_values = [
            category,
            hierarchy.get("section_group", ""),
            document.section_name,
        ]
        search_lines = searchable_text.splitlines()
        for value in source_values:
            normalized_value = str(value or "").casefold()
            if normalized_value and normalized_value not in search_lines:
                search_lines.append(normalized_value)
        searchable_text = "\n".join(search_lines)
        KnowledgeSearchEntry.objects.filter(pk=entry.pk).update(
            category=category,
            searchable_text=searchable_text,
        )


class Migration(migrations.Migration):
    dependencies = [
        ("knowledge", "0003_alter_knowledgesearchentry_curation_status"),
    ]

    operations = [
        migrations.RunPython(
            backfill_onenote_section_categories,
            migrations.RunPython.noop,
        ),
    ]
