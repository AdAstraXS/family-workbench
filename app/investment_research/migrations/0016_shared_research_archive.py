from django.db import migrations, models
from investment_research.archive_bridge import link_saved_sec_materials


class Migration(migrations.Migration):
    dependencies = [("investment_research", "0015_researchdossier_is_watched")]
    operations = [
        migrations.AddField(model_name="researchthesisrevision", name="hypothesis_context",
            field=models.JSONField("假设的反证与跟踪背景", default=list, blank=True, db_default=[])),
        migrations.RunPython(link_saved_sec_materials, migrations.RunPython.noop),
    ]
