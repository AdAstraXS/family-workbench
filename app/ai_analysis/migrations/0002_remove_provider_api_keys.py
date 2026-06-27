from django.db import migrations


SENSITIVE_EXTRA_KEYS = ("api_key", "apikey", "secret_key", "access_token", "token")


def remove_provider_api_keys(apps, schema_editor):
    AiProvider = apps.get_model("ai_analysis", "AiProvider")
    for provider in AiProvider.objects.all():
        extra_data = provider.extra_data or {}
        if not isinstance(extra_data, dict):
            continue

        cleaned_data = extra_data.copy()
        removed = False
        for key in SENSITIVE_EXTRA_KEYS:
            if key in cleaned_data:
                cleaned_data.pop(key, None)
                removed = True

        if removed:
            provider.extra_data = cleaned_data
            provider.save(update_fields=["extra_data"])


class Migration(migrations.Migration):

    dependencies = [
        ("ai_analysis", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(remove_provider_api_keys, migrations.RunPython.noop),
    ]
