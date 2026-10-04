"""Local disposable PostgreSQL, same credentials and port as the watch CI service."""
from .settings_quality_test import *
from .settings_watch_pg_test import DATABASES, SILENCED_SYSTEM_CHECKS
