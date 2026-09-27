from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('intelligence', '0010_alter_programentry_state')]
    operations = [
        migrations.AddField(model_name='programentry', name='asr_attempt_history', field=models.JSONField(default=list, blank=True)),
        migrations.AddField(model_name='programentry', name='asr_error_code', field=models.CharField(max_length=80, blank=True)),
        migrations.AddField(model_name='programentry', name='retry_audio_transfer', field=models.BooleanField(default=False)),
    ]
