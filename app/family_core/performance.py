"""Request measurements without URLs, query values, identity or SQL text."""
import logging
import time
from contextlib import ExitStack

from django.db import connections
from .household import household_request_cache

logger = logging.getLogger("workbench.performance")


class RequestPerformanceMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path.startswith(("/static/", "/media/")):
            return self.get_response(request)
        start = time.perf_counter()
        count, database_ms = 0, 0.0

        def measure(execute, sql, params, many, context):
            nonlocal count, database_ms
            query_start = time.perf_counter()
            try:
                return execute(sql, params, many, context)
            finally:
                count += 1
                database_ms += (time.perf_counter() - query_start) * 1000

        response = None
        try:
            with household_request_cache(), ExitStack() as stack:
                for db in connections.all():
                    stack.enter_context(db.execute_wrapper(measure))
                response = self.get_response(request)
            return response
        finally:
            match = getattr(request, "resolver_match", None)
            route = match.view_name if match else "unresolved"
            size = len(response.content) if response is not None and not response.streaming else -1
            logger.info("request route=%s method=%s status=%s elapsed_ms=%.1f queries=%d db_ms=%.1f bytes=%d",
                        route, request.method, response.status_code if response is not None else 500,
                        (time.perf_counter() - start) * 1000, count, database_ms, size)

