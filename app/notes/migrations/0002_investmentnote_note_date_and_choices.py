import django.utils.timezone
from django.db import migrations, models


def normalize_note_types(apps, schema_editor):
    InvestmentNote = apps.get_model("notes", "InvestmentNote")
    InvestmentNote.objects.filter(note_type="general").update(note_type="other")


class Migration(migrations.Migration):

    dependencies = [
        ("notes", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="investmentnote",
            name="note_date",
            field=models.DateField(default=django.utils.timezone.localdate, verbose_name="笔记日期"),
        ),
        migrations.RunPython(normalize_note_types, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="investmentnote",
            name="note_type",
            field=models.CharField(
                choices=[
                    ("trade", "交易记录"),
                    ("strategy", "投资策略"),
                    ("research", "研究分析"),
                    ("psychology", "投资心理"),
                    ("other", "其他"),
                ],
                default="other",
                max_length=50,
                verbose_name="笔记类型",
            ),
        ),
        migrations.AlterField(
            model_name="investmentnote",
            name="visibility",
            field=models.CharField(
                choices=[("private", "仅自己"), ("family", "家庭共享")],
                default="private",
                max_length=20,
                verbose_name="可见范围",
            ),
        ),
        migrations.AlterField(
            model_name="investmentnote",
            name="content",
            field=models.TextField(verbose_name="内容"),
        ),
        migrations.AlterModelOptions(
            name="investmentnote",
            options={
                "ordering": ["-note_date", "-updated_at"],
                "verbose_name": "投资笔记",
                "verbose_name_plural": "投资笔记",
            },
        ),
    ]
