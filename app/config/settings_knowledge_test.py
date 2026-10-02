"""Knowledge tests: offline by default, optional isolated PostgreSQL container only.

Never imports production settings or dotenv. KNOWLEDGE_TEST_POSTGRES=1 targets
the disposable web-capture-test-db container on a dedicated local Docker network.
"""
import os
from .settings_research_test import *  # noqa: F403

if os.environ.get("KNOWLEDGE_TEST_POSTGRES") == "1":
    DATABASES = {"default": {"ENGINE": "django.db.backends.postgresql", "NAME": "web_capture_test", "USER": "postgres", "HOST": "web-capture-test-db", "PORT": 5432}}
