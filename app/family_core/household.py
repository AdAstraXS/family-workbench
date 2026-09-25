from .models import Family, SiteSetting


def get_household_family():
    """Compatibility bridge while legacy family foreign keys are removed in stages."""
    return Family.objects.order_by("pk").first()


def get_site_setting():
    # Reading defaults must not insert a singleton from a GET page.
    return SiteSetting.objects.filter(pk=1).first() or SiteSetting(pk=1)
