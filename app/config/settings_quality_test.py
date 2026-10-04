"""Shared offline checks: no dotenv, production credentials or live services."""
from .settings_watch_test import *

if 'ledger.middleware.FinancialReportMiddleware' not in MIDDLEWARE:
    MIDDLEWARE = [*MIDDLEWARE, 'ledger.middleware.FinancialReportMiddleware']
MIDDLEWARE = ['family_core.performance.RequestPerformanceMiddleware', *MIDDLEWARE]
