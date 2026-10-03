"""Apply the owner's 2026-10-03 approval to the existing Pro research budget only."""
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

from django.db import migrations


def apply_budget(apps, schema_editor):
    Provider = apps.get_model('ai_analysis', 'AiProvider')
    for provider in Provider.objects.using(schema_editor.connection.alias).filter(
            model_name='deepseek-v4-pro', is_active=True):
        if urlsplit(provider.base_url).hostname != 'api.deepseek.com':
            continue
        data = dict(provider.extra_data or {})
        try:
            previous = Decimal(str(data.get('research_max_estimated_usd', '0')))
        except InvalidOperation:
            continue
        if not data.get('allow_research_analysis') or previous != Decimal('0.10'):
            continue
        data['research_max_estimated_usd'] = '0.50'
        data['research_budget_approval'] = 'owner-approved-2026-10-03-pro-0.50'
        provider.extra_data = data
        provider.save(update_fields=['extra_data'])


class Migration(migrations.Migration):
    dependencies = [('investment_research', '0017_researchprompttemplate'),
                    ('ai_analysis', '0011_module_model_defaults')]
    operations = [migrations.RunPython(apply_budget, migrations.RunPython.noop)]
