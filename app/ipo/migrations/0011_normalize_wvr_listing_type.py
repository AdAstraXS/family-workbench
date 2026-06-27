from django.db import migrations


def normalize_wvr_listing_type(apps, schema_editor):
    option_model = apps.get_model("ipo", "HkIpoListingOption")
    listing_model = apps.get_model("ipo", "HkIpoListing")
    canonical, _ = option_model.objects.get_or_create(
        category="listing_type",
        code="wvr",
        defaults={
            "name": "同股不同权",
            "sort_order": 50,
            "is_active": True,
        },
    )
    legacy_options = option_model.objects.filter(
        category="listing_type",
        name="同股不同权",
    ).exclude(pk=canonical.pk)
    for option in legacy_options:
        listing_model.objects.filter(listing_type=option.code).update(
            listing_type=canonical.code
        )
    legacy_options.delete()


class Migration(migrations.Migration):

    dependencies = [
        ("ipo", "0010_hk_connect_rules"),
    ]

    operations = [
        migrations.RunPython(normalize_wvr_listing_type, migrations.RunPython.noop),
    ]
