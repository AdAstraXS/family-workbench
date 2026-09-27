from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("reading", "0002_bookfile_page_count_annotation_annotationcomment_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="annotation",
            name="highlight_visible",
            field=models.BooleanField(default=True),
        ),
    ]
