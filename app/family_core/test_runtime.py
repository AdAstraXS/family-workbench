import threading
from unittest.mock import patch
from django.db import connection, connections
from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase, override_settings
from django.core.management.base import CommandError
from .job_runtime import job_slot, JobCapacityError, JobDeadline, BoundedJobCommand
from .performance import RequestPerformanceMiddleware


class PerformanceLoggingTests(SimpleTestCase):
    def test_logs_route_metrics_without_private_parameters(self):
        request = RequestFactory().get('/search/?q=private-finances')
        request.resolver_match = type('Match', (), {'view_name': 'knowledge:search'})()
        with self.assertLogs('workbench.performance', 'INFO') as captured:
            response = RequestPerformanceMiddleware(lambda r: HttpResponse('ok'))(request)
        self.assertEqual(response.status_code, 200)
        self.assertIn('route=knowledge:search', captured.output[0])
        self.assertIn('bytes=2', captured.output[0])
        self.assertNotIn('private-finances', captured.output[0])

    def test_capacity_rejection_updates_business_record_without_running(self):
        command = BoundedJobCommand()
        with patch('family_core.job_runtime.job_slot', side_effect=JobCapacityError('busy')), patch('family_core.job_runtime.record_failure') as failure:
            with self.assertRaises(CommandError):
                command.execute(request_id=12)
        failure.assert_called_once_with('job_runtime', {'request_id': 12}, 'busy', started=False)

    def test_deadline_releases_slot_and_records_failure(self):
        from contextlib import contextmanager
        released = []
        @contextmanager
        def slot():
            try:
                yield
            finally:
                released.append(True)
        with patch('family_core.job_runtime.job_slot', slot), patch('django.core.management.base.BaseCommand.execute', side_effect=JobDeadline()), patch('family_core.job_runtime.record_failure') as failure:
            with self.assertRaises(CommandError):
                BoundedJobCommand().execute(request_id=12)
        self.assertEqual(released, [True])
        self.assertTrue(failure.call_args.kwargs['started'])


class JobSlotConcurrencyTests(SimpleTestCase):
    # Advisory locks change no rows; avoid flushing migration seed data.
    databases = {'default'}
    @override_settings(BACKGROUND_JOB_SLOTS=1)
    def test_other_connection_is_blocked_and_can_run_after_release(self):
        if connection.vendor != 'postgresql':
            self.skipTest('requires production-compatible advisory locks')
        outcomes = []
        def attempt():
            try:
                with job_slot():
                    outcomes.append('entered')
            except JobCapacityError:
                outcomes.append('busy')
            finally:
                connections.close_all()
        with job_slot():
            thread = threading.Thread(target=attempt)
            thread.start()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        thread = threading.Thread(target=attempt)
        thread.start()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcomes, ['busy', 'entered'])
