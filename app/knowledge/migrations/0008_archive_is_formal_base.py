from django.db import migrations, models
from django.db.models import F


def align_existing_knowledge_states(apps, schema_editor):
    KnowledgeDocument = apps.get_model("knowledge", "KnowledgeDocument")
    KnowledgeSearchEntry = apps.get_model("knowledge", "KnowledgeSearchEntry")

    # 旧版未完成人工确认的 OneNote 页面不再默认占据精选知识。
    onenote_documents = KnowledgeDocument.objects.filter(
        source__kind="onenote",
    ).exclude(curation_status="confirmed")
    onenote_documents.update(library_tier="archive")
    onenote_documents.filter(category=F("section_name")).update(category="")

    # 已勾选随手记从“直接入库”改为“归档并加入待整理”。
    KnowledgeSearchEntry.objects.filter(item_kind="investment_note").update(
        knowledge_status="pending",
        curation_status="inbox",
    )


class Migration(migrations.Migration):
    dependencies = [
        ("notes", "0005_investmentnote_knowledge_state"),
        ("knowledge", "0007_knowledgedocument_library_tier_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="knowledgedocument",
            name="knowledge_status",
            field=models.CharField(
                choices=[
                    ("pending", "待整理"),
                    ("included", "归档资料"),
                    ("archived", "仅同步"),
                ],
                default="included",
                max_length=20,
                verbose_name="知识状态",
            ),
        ),
        migrations.AlterField(
            model_name="knowledgedocument",
            name="library_tier",
            field=models.CharField(
                choices=[("knowledge", "精选知识"), ("archive", "仅归档")],
                db_index=True,
                default="archive",
                max_length=20,
                verbose_name="精选状态",
            ),
        ),
        migrations.AlterField(
            model_name="knowledgesearchentry",
            name="knowledge_status",
            field=models.CharField(
                choices=[
                    ("pending", "待整理"),
                    ("included", "归档资料"),
                    ("archived", "仅同步"),
                ],
                default="included",
                max_length=20,
                verbose_name="知识状态",
            ),
        ),
        migrations.RunPython(
            align_existing_knowledge_states,
            migrations.RunPython.noop,
        ),
    ]
