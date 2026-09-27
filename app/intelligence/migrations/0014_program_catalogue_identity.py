from django.db import migrations


def populate_catalogue_identity(apps, schema_editor):
    Subscription = apps.get_model('intelligence', 'ProgramSubscription')
    Family = apps.get_model('family_core', 'Family')
    sources = {
        'rhino': ('视野环球财经', 'https://www.youtube.com/@RhinoFinance'),
        'dwarkesh': ('Dwarkesh Podcast', 'https://www.dwarkesh.com/feed'),
        'good_company': ('In Good Company', 'https://feeds.acast.com/public/shows/in-good-company-with-nicolai-tangen'),
        'oaktree': ('Howard Marks · Oaktree Memos', 'https://www.oaktreecapital.com/insights'),
    }
    for family_id in Family.objects.values_list('pk', flat=True).iterator():
        for code, (name, url) in sources.items():
            subscription, _ = Subscription.objects.get_or_create(
                family_id=family_id, code=code, defaults={
                    'custom_name': name, 'source_url': url, 'enabled': False})
            updates = {}
            if not subscription.custom_name:
                updates['custom_name'] = name
            if not subscription.source_url:
                updates['source_url'] = url
            if updates:
                Subscription.objects.filter(pk=subscription.pk).update(**updates)


class Migration(migrations.Migration):
    dependencies = [('intelligence', '0013_program_subscription_removal')]
    operations = [migrations.RunPython(populate_catalogue_identity, migrations.RunPython.noop)]
