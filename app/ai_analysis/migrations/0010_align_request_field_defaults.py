"""Match the request model to the applied global AI migration state."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("ai_analysis", "0009_merge_request_compatibility")]

    operations = [
        migrations.AlterField(
            model_name="aianalysisrequest",
            name="execution_token",
            field=models.CharField("执行令牌", max_length=64, blank=True),
        ),
        migrations.AlterField(
            model_name="aianalysisrequest",
            name="idempotency_key",
            field=models.CharField("幂等键", max_length=100, blank=True),
        ),
        migrations.AlterField(
            model_name="aianalysisrequest",
            name="request_fingerprint",
            field=models.CharField("请求指纹", max_length=64, blank=True),
        ),
    ]
