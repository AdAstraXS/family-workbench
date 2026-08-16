from django.db import migrations, models


def initialize_knowledge_status(apps, schema_editor):
    KnowledgeDocument = apps.get_model("knowledge", "KnowledgeDocument")
    KnowledgeSearchEntry = apps.get_model("knowledge", "KnowledgeSearchEntry")

    KnowledgeDocument.objects.all().update(knowledge_status="included")
    KnowledgeDocument.objects.filter(
        curation_status__in=["ignored", "archived"]
    ).update(knowledge_status="archived")

    for document in KnowledgeDocument.objects.only("id", "knowledge_status").iterator():
        KnowledgeSearchEntry.objects.filter(document_id=document.pk).update(
            knowledge_status=document.knowledge_status
        )

    # Existing随手记没有做过“加入知识库”的明确选择。原笔记继续保留，
    # 这里只移除可重建的知识搜索投影，之后勾选时会自动重建。
    KnowledgeSearchEntry.objects.filter(item_kind="investment_note").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("notes", "0004_investmentnote_include_in_knowledge"),
        ("knowledge", "0004_backfill_onenote_section_categories"),
    ]

    operations = [
        migrations.AddField(
            model_name="knowledgedocument",
            name="knowledge_status",
            field=models.CharField(
                choices=[
                    ("pending", "待整理"),
                    ("included", "已入库"),
                    ("archived", "仅同步归档"),
                ],
                default="included",
                max_length=20,
                verbose_name="知识状态",
            ),
        ),
        migrations.AddField(
            model_name="knowledgesearchentry",
            name="knowledge_status",
            field=models.CharField(
                choices=[
                    ("pending", "待整理"),
                    ("included", "已入库"),
                    ("archived", "仅同步归档"),
                ],
                default="included",
                max_length=20,
                verbose_name="知识状态",
            ),
        ),
        migrations.AddIndex(
            model_name="knowledgedocument",
            index=models.Index(
                fields=["family", "knowledge_status"],
                name="knowledge_k_family__70fe55_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="knowledgesearchentry",
            index=models.Index(
                fields=["family", "knowledge_status"],
                name="knowledge_k_family__0f603c_idx",
            ),
        ),
        migrations.RunPython(
            initialize_knowledge_status,
            migrations.RunPython.noop,
        ),
    ]
