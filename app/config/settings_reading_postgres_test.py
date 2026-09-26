"""Only for the network-isolated disposable PostgreSQL reading test container.

No .env or production configuration is loaded. Run the Python container in the
test PostgreSQL container's network namespace; never use against a live server.
"""
from .settings_research_test import *

DATABASES = {"default": {"ENGINE":"django.db.backends.postgresql", "HOST":"127.0.0.1",
    "PORT":"5432", "NAME":"postgres", "USER":"postgres", "PASSWORD":"",
    "TEST":{"NAME":"test_reading_disposable"}}}
