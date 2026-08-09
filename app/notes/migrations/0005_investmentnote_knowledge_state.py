from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("notes", "0004_investmentnote_include_in_knowledge"),
    ]

    operations = [
        migrations.AddField(
            model_name="investmentnote",
            name="knowledge_state",
            field=models.CharField(
                choices=[("pending", "待整理"), ("curated", "精选知识")],
                default="pending",
                max_length=20,
                verbose_name="知识整理状态",
            ),
        ),
    ]
