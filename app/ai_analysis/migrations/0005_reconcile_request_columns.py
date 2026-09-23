"""Keep request inserts compatible with databases that ran global AI migrations."""

from django.db import migrations, models


FIELDS = (
    ("idempotency_key", "幂等键", 100),
    ("request_fingerprint", "请求指纹", 64),
    ("execution_token", "执行令牌", 64),
)


def add_missing_columns(apps, schema_editor):
    request_model = apps.get_model("ai_analysis", "AiAnalysisRequest")
    table_name = request_model._meta.db_table
    with schema_editor.connection.cursor() as cursor:
        existing = {
            column.name
            for column in schema_editor.connection.introspection.get_table_description(cursor, table_name)
        }
    for name, label, max_length in FIELDS:
        if name in existing:
            continue
        quote = schema_editor.quote_name
        schema_editor.execute(
            f"ALTER TABLE {quote(table_name)} ADD COLUMN {quote(name)} "
            f"varchar({max_length}) NOT NULL DEFAULT ''"
        )


class Migration(migrations.Migration):
    dependencies = [("ai_analysis", "0004_add_zhipu_vision_provider")]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[migrations.RunPython(add_missing_columns, migrations.RunPython.noop)],
            state_operations=[
                migrations.AddField(
                    model_name="aianalysisrequest",
                    name=name,
                    field=models.CharField(label, max_length=max_length, blank=True, default=""),
                )
                for name, label, max_length in FIELDS
            ],
        ),
    ]
