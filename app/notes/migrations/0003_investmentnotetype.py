import django.db.models.deletion
from django.db import migrations, models


DEFAULT_TYPES = [
    ("trade", "交易记录", 10),
    ("strategy", "投资策略", 20),
    ("research", "研究分析", 30),
    ("psychology", "投资心理", 40),
    ("other", "其他", 90),
]


def create_types_and_link_notes(apps, schema_editor):
    InvestmentNoteType = apps.get_model("notes", "InvestmentNoteType")
    InvestmentNote = apps.get_model("notes", "InvestmentNote")

    types_by_code = {}
    for code, name, sort_order in DEFAULT_TYPES:
        note_type, _created = InvestmentNoteType.objects.get_or_create(
            code=code,
            defaults={
                "name": name,
                "sort_order": sort_order,
                "is_active": True,
            },
        )
        types_by_code[code] = note_type

    for code in InvestmentNote.objects.values_list("note_type", flat=True).distinct():
        normalized_code = code or "other"
        note_type = types_by_code.get(normalized_code)
        if note_type is None:
            note_type, _created = InvestmentNoteType.objects.get_or_create(
                code=normalized_code,
                defaults={
                    "name": normalized_code,
                    "sort_order": 100,
                    "is_active": True,
                },
            )
            types_by_code[normalized_code] = note_type
        InvestmentNote.objects.filter(note_type=code).update(note_type_ref=note_type)


def restore_type_codes(apps, schema_editor):
    InvestmentNote = apps.get_model("notes", "InvestmentNote")
    for note in InvestmentNote.objects.select_related("note_type_ref").iterator():
        note.note_type = note.note_type_ref.code
        note.save(update_fields=["note_type"])


class Migration(migrations.Migration):

    dependencies = [
        ("notes", "0002_investmentnote_note_date_and_choices"),
    ]

    operations = [
        migrations.CreateModel(
            name="InvestmentNoteType",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(auto_now_add=True, verbose_name="创建时间"),
                ),
                (
                    "updated_at",
                    models.DateTimeField(auto_now=True, verbose_name="更新时间"),
                ),
                ("name", models.CharField(max_length=50, unique=True, verbose_name="类型名称")),
                (
                    "code",
                    models.SlugField(
                        help_text="用于筛选和样式识别，建议使用简短的小写英文，保存后尽量不要修改。",
                        max_length=50,
                        unique=True,
                        verbose_name="类型编码",
                    ),
                ),
                ("sort_order", models.PositiveIntegerField(default=100, verbose_name="排序")),
                ("is_active", models.BooleanField(default=True, verbose_name="是否启用")),
                ("remark", models.CharField(blank=True, max_length=200, verbose_name="备注")),
            ],
            options={
                "verbose_name": "投资笔记类型",
                "verbose_name_plural": "投资笔记类型",
                "ordering": ["sort_order", "id"],
            },
        ),
        migrations.AddField(
            model_name="investmentnote",
            name="note_type_ref",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="notes.investmentnotetype",
            ),
        ),
        migrations.RunPython(create_types_and_link_notes, restore_type_codes),
        migrations.RemoveIndex(
            model_name="investmentnote",
            name="notes_inves_family__6b6e39_idx",
        ),
        migrations.RemoveField(
            model_name="investmentnote",
            name="note_type",
        ),
        migrations.RenameField(
            model_name="investmentnote",
            old_name="note_type_ref",
            new_name="note_type",
        ),
        migrations.AlterField(
            model_name="investmentnote",
            name="note_type",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="investment_notes",
                to="notes.investmentnotetype",
                verbose_name="笔记类型",
            ),
        ),
        migrations.AddIndex(
            model_name="investmentnote",
            index=models.Index(
                fields=["family", "member", "note_type", "created_at"],
                name="notes_inves_family__61f92d_idx",
            ),
        ),
    ]
