from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from family_core.models import Family, FamilyMember
from family_core.workspace import MODULES
from knowledge.models import KnowledgeDocument, KnowledgeSource
from ledger.models import AssetBalanceEntry, AssetBalanceSnapshot
from .presentation import homepage_details


class SongtingDashboardTests(TestCase):
    def setUp(self):
        self.family = Family.objects.create(name="Synthetic Songting family")
        self.user = get_user_model().objects.create_user(username="songting-member")
        self.member = FamilyMember.objects.create(family=self.family, user=self.user, display_name="Test member")
        self.client.force_login(self.user)

    def snapshot(self, day, amount, **kwargs):
        snapshot = AssetBalanceSnapshot.objects.create(family=self.family, snapshot_date=date(2026, 9, day), **kwargs)
        AssetBalanceEntry.objects.create(snapshot=snapshot, member=self.member, base_amount=Decimal(amount))
        return snapshot

    def test_trend_excludes_drafts_and_other_currencies_without_recomputing_saved_amounts(self):
        self.snapshot(1, "100.12")
        self.snapshot(2, "999999", is_draft=True)
        self.snapshot(3, "88888", base_currency="USD")
        latest = self.snapshot(4, "120.34")
        data = homepage_details(self.family, self.member, latest, date(2026, 9, 25))
        self.assertEqual([s.recorded_total for s in data["asset_trend"]], [Decimal("100.12"), Decimal("120.34")])
        self.assertEqual(data["trend_points"], "20.00,165.00 560.00,25.00")
        self.assertEqual(data["asset_allocation"][0]["amount"], Decimal("120.34"))

    def test_empty_and_negative_balances_are_not_fabricated_into_a_pie(self):
        data = homepage_details(self.family, self.member, None, date(2026, 9, 25))
        self.assertNotIn("trend_points", data)
        latest = self.snapshot(2, "-100")
        data = homepage_details(self.family, self.member, latest, date(2026, 9, 25))
        self.assertFalse(data["allocation_can_draw"])
        self.assertNotIn("trend_points", data)

    def test_home_does_not_disclose_other_members_private_knowledge(self):
        other = FamilyMember.objects.create(family=self.family, display_name="Other")
        source = KnowledgeSource.objects.create(family=self.family, owner=other, key="private-songting", kind="onenote", name="Private", visibility="private")
        KnowledgeDocument.objects.create(family=self.family, owner=other, source=source, external_id="private", title="PRIVATE_SONGTING_TITLE", visibility="private", library_tier=KnowledgeDocument.LIBRARY_KNOWLEDGE, knowledge_status=KnowledgeDocument.KNOWLEDGE_INCLUDED)
        response = self.client.get(reverse("dashboard:home"))
        self.assertNotContains(response, "PRIVATE_SONGTING_TITLE")
        self.assertEqual(list(response.context["recent_knowledge"]), [])

    def test_home_includes_all_existing_modules_and_reads_without_writing(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(reverse("dashboard:home"))
        self.assertEqual(response.status_code, 200)
        for app, view, *_ in MODULES:
            self.assertContains(response, reverse(f"{app}:{view}"))
        self.assertContains(response, "功能筹备中")
        writes = [q["sql"] for q in queries if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))]
        self.assertEqual(writes, [])
        self.assertContains(response, "投资资产与全资产快照存在重叠，不相加")
        self.assertNotContains(response, "4,860,000")

    def test_theme_shell_is_available_on_authenticated_pages_and_login(self):
        response = self.client.get(reverse("ledger:overview"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'aria-label="主导航"')
        self.assertContains(response, 'data-ws-palette="warm"')
        self.client.logout()
        response = self.client.get(reverse("login"))
        self.assertContains(response, "workspace-preferences.js")
        self.assertNotContains(response, 'id="ws-sidebar"')
