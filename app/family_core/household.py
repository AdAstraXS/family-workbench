from contextlib import contextmanager
from contextvars import ContextVar

from .models import Family, SiteSetting

_request_data = ContextVar("household_request_data", default=None)


@contextmanager
def household_request_cache():
    token = _request_data.set({})
    try:
        yield
    finally:
        _request_data.reset(token)


def get_household_family():
    """Compatibility bridge while legacy family foreign keys are removed in stages."""
    return Family.objects.order_by("pk").first()


def get_site_setting():
    # Reading defaults must not insert a singleton from a GET page.
    cache = _request_data.get()
    if cache is None:
        return SiteSetting.objects.filter(pk=1).first() or SiteSetting(pk=1)
    if "site_setting" not in cache:
        cache["site_setting"] = SiteSetting.objects.filter(pk=1).first() or SiteSetting(pk=1)
    return cache["site_setting"]
