from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("knowledge", "0008_archive_is_formal_base"),
    ]

    operations = [
        migrations.AddField(
            model_name="knowledgeimportbatch",
            name="person_name",
            field=models.CharField(
                blank=True,
                help_text="填写后统一作为本批次文章的作者；HTML 原始署名仍保留在逐篇记录中。",
                max_length=300,
                verbose_name="归属人物",
            ),
        ),
    ]
