"""Isolated local test database; never reuse the application database as TEST.NAME."""
import os

from .settings import *  # noqa: F403

DATABASES["default"]["TEST"] = {"NAME": os.environ.get("WORKBENCH_TEST_DB", "test_workbench_hardening")}  # noqa: F405
if not DATABASES["default"]["TEST"]["NAME"].startswith("test_"):  # noqa: F405
    raise ValueError("WORKBENCH_TEST_DB must start with test_")
if DATABASES["default"]["TEST"]["NAME"] == DATABASES["default"]["NAME"]:  # noqa: F405
    raise ValueError("The test database must differ from the application database")
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
