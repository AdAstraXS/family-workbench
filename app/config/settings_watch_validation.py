"""Separate loopback database for real-source acceptance; no production dotenv."""

from .settings_watch_local import *

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": LOCAL_ROOT / "validation-live-sources.sqlite3",
    }
}
# Paid acceptance enables this only inside its explicit, bounded command process.
INVESTMENT_WATCH_MODEL_ENABLED = False
INVESTMENT_WATCH_DAILY_CNY = "10"
INVESTMENT_WATCH_MONTHLY_CNY = "10"
