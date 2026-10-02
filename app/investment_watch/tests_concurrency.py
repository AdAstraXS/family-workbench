from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier
from unittest import skipUnless
from django.db import connection, connections
from django.test import TransactionTestCase
from django.contrib.auth import get_user_model
from ai_analysis.models import AiProvider
from family_core.models import Family, FamilyMember
from .budget import reserve, budget_status
from .models import BudgetReceipt
from .services import WatchError, idempotent
from .worker import acquire


@skipUnless(connection.vendor == "postgresql", "Row-lock guarantees require PostgreSQL")
class ConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.family = Family.objects.create(name="Concurrent budget")
        self.member = FamilyMember.objects.create(
            family=self.family,
            display_name="Owner",
            user=get_user_model().objects.create(username="parallel-local-test"),
        )
        self.provider = AiProvider.objects.create(
            name="Budget test", provider_type="openai_compatible", model_name="test"
        )

    def parallel(self, action):
        barrier = Barrier(2)

        def run(index):
            try:
                barrier.wait(timeout=10)
                return action(index)
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            return list(pool.map(run, range(2)))

    def test_two_reservations_cannot_exceed_daily_cap(self):
        def action(index):
            try:
                reserve(self.member, self.provider, str(index) * 64, Decimal(".6"))
                return "reserved"
            except WatchError:
                return "blocked"

        self.assertCountEqual(self.parallel(action), ["reserved", "blocked"])
        self.assertEqual(budget_status(self.family)["daily"], Decimal(".6"))
        self.assertEqual(BudgetReceipt.objects.count(), 1)

    def test_worker_lease_has_one_winner(self):
        self.assertEqual(
            sum(bool(x) for x in self.parallel(lambda _: acquire(self.family))), 1
        )

    def test_duplicate_operations_execute_once(self):
        def action(_):
            return idempotent(
                self.member,
                "concurrent",
                "identical-key",
                {"a": 1},
                lambda: {"count": BudgetReceipt.objects.count()},
            )

        self.assertEqual(self.parallel(action), [{"count": 0}, {"count": 0}])
        from .models import OperationReceipt

        self.assertEqual(OperationReceipt.objects.count(), 1)
