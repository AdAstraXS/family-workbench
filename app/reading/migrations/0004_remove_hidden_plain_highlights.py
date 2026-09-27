from django.db import migrations


def remove_hidden_plain_highlights(apps, schema_editor):
    Annotation = apps.get_model("reading", "Annotation")
    AnnotationComment = apps.get_model("reading", "AnnotationComment")
    for item in Annotation.objects.filter(highlight_visible=False).iterator():
        if item.note or AnnotationComment.objects.filter(annotation_id=item.pk).exists():
            Annotation.objects.filter(pk=item.pk).update(highlight_visible=True)
        else:
            item.delete()


class Migration(migrations.Migration):
    dependencies = [
        ("reading", "0003_annotation_highlight_visible"),
    ]

    operations = [migrations.RunPython(remove_hidden_plain_highlights, migrations.RunPython.noop)]
