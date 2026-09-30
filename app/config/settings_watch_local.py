"""Loopback preview only. Never imports dotenv or the production database."""

from .settings_watch_test import *

DEBUG = True
WATCH_LOCAL_PREVIEW = True
SECRET_KEY = "local-watch-preview-only-not-for-network-use"
ALLOWED_HOSTS = ["localhost", "127.0.0.1"]
LOCAL_ROOT = BASE_DIR.parent / ".watch-local"
LOCAL_ROOT.mkdir(exist_ok=True)
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": LOCAL_ROOT / "preview.sqlite3",
    }
}
INVESTMENT_WATCH_COLLECT_ENABLED = True
INVESTMENT_WATCH_MODEL_ENABLED = False
SILENCED_SYSTEM_CHECKS = ["knowledge.W001", "knowledge.W002"]
STATIC_URL = "/static/"
PASSWORD_HASHERS = ["django.contrib.auth.hashers.PBKDF2PasswordHasher"]
