from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [('intelligence', '0012_self_service_program_sources')]
    operations = [
        migrations.AddField(model_name='programsubscription', name='removed_at',
                            field=models.DateTimeField(null=True, blank=True)),
        migrations.AddField(model_name='programsubscription', name='removed_by',
                            field=models.ForeignKey(to='family_core.familymember', null=True, blank=True,
                                on_delete=django.db.models.deletion.SET_NULL,
                                related_name='removed_program_subscriptions')),
    ]
