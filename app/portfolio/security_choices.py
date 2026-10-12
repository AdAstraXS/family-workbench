"""Security choices for portfolio entry forms; not historical holdings or pricing."""

from .models import Security


def watchlist_security_choices(family_id):
    if not family_id:
        return Security.objects.none()
    return Security.objects.filter(
        watchlist_items__family_id=family_id,
        watchlist_items__is_active=True,
    ).exclude(asset_type=Security.TYPE_OPTION).distinct().order_by("market", "symbol")
