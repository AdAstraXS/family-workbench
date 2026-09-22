from django.core.management.base import BaseCommand, CommandError

from family_core.models import Family
from option_wheel.models import WheelWatchItem
from option_wheel.watch_refresh import refresh_watch_events, refresh_watch_prices


class Command(BaseCommand):
    help = "Update option watchlist price and upcoming events from Futu (no trades)."

    def handle(self, *args, **options):
        families = Family.objects.filter(pk__in=WheelWatchItem.objects.values_list("family_id", flat=True))
        for family in families:
            try:
                prices = refresh_watch_prices(family, update_names=True)
                events = refresh_watch_events(family)
            except Exception as exc:
                raise CommandError(f"期权自选股更新失败：{type(exc).__name__}") from exc
            self.stdout.write(f"family={family.pk} prices={prices} events={events}")
