from django.db import migrations, models


def existing_observations(apps, schema_editor):
    Dossier = apps.get_model('investment_research', 'ResearchDossier')
    Preparation = apps.get_model('investment_research', 'ResearchPreparation')
    for dossier in Dossier.objects.all().iterator():
        latest = Preparation.objects.filter(dossier=dossier).order_by('-updated_at', '-pk').first()
        if latest and latest.decision == 'watch':
            Dossier.objects.filter(pk=dossier.pk).update(is_watched=True)


class Migration(migrations.Migration):
    dependencies = [('investment_research', '0014_researchpreparation')]
    operations = [migrations.AddField(model_name='researchdossier', name='is_watched',
                  field=models.BooleanField(default=False, db_default=False, verbose_name='加入观察')),
                  migrations.RunPython(existing_observations, migrations.RunPython.noop)]
