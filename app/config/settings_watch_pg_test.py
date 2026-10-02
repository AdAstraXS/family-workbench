"""Disposable loopback PostgreSQL only; never load production credentials."""

import os
from .settings_watch_test import *

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "watch_test",
        "USER": "watch_test",
        "PASSWORD": "watch-test-local-only",
        "HOST": "127.0.0.1",
        "PORT": os.getenv("WATCH_TEST_PG_PORT", "55439"),
        "TEST": {"NAME": "test_watch_test"},
    }
}
SILENCED_SYSTEM_CHECKS = ["knowledge.W001", "knowledge.W002"]
