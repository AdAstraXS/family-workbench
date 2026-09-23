from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("investment_research", "0003_officialresearchcontentversion")]

    operations = [
        migrations.AlterField(
            model_name="researchdossier", name="initial_thesis",
            field=models.TextField(blank=True, verbose_name="原始持有理由"),
        ),
    ]
