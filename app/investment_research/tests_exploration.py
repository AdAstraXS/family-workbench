from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from family_core.models import Family, FamilyMember
from portfolio.models import Security

from .models import ResearchDossier, ResearchThesisRevision
from .services import (
    DuplicateDossier, ThesisRevisionConflict,
    create_exploration, save_first_thesis,
)


class ExplorationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.family = Family.objects.create(name="A family")
        cls.other_family = Family.objects.create(name="B family")
        cls.security = Security.objects.create(symbol="TSLA", name="Tesla", market="US", asset_type="stock")
        cls.other_security = Security.objects.create(symbol="AAPL", name="Apple", market="US", asset_type="stock")
        alice = get_user_model().objects.create_user(username="explore-a", password="x")
        bob = get_user_model().objects.create_user(username="explore-b", password="x")
        cls.alice = FamilyMember.objects.create(user=alice, family=cls.family, display_name="Alice")
        cls.bob = FamilyMember.objects.create(user=bob, family=cls.other_family, display_name="Bob")

    def test_new_company_needs_no_thesis_or_position(self):
        self.client.force_login(self.alice.user)
        url = reverse("investment_research:explore")
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(ResearchDossier.objects.count(), 0)
        response = self.client.post(url, {"security": self.security.pk})
        self.assertEqual(response.status_code, 302)
        dossier = ResearchDossier.objects.get(owner=self.alice, security=self.security)
        self.assertEqual(dossier.initial_thesis, "")
        self.assertIsNone(dossier.current_revision)
        self.assertEqual(dossier.revisions.count(), 0)
        self.assertContains(self.client.get(response.url), "正在了解这家公司")
        self.assertContains(self.client.get(reverse("investment_research:index")), "尚无正式判断")
        self.client.post(url, {"security": self.security.pk})
        self.assertEqual(ResearchDossier.objects.count(), 1)

    def test_first_revision_is_atomic_and_repeat_conflicts(self):
        dossier = create_exploration(actor=self.alice, security=self.security)
        first_url = reverse("investment_research:first_thesis", args=[dossier.pk])
        self.client.force_login(self.alice.user)
        self.assertEqual(self.client.get(first_url).status_code, 200)
        self.assertIsNone(ResearchDossier.objects.get(pk=dossier.pk).current_revision)
        data = {"thesis": "现金流可能改善", "pillars": "增长可持续", "questions": "资本支出何时回收"}
        self.assertEqual(self.client.post(first_url, data).status_code, 302)
        dossier.refresh_from_db()
        self.assertEqual(dossier.initial_thesis, "现金流可能改善")
        self.assertEqual(dossier.current_revision.revision_number, 1)
        self.assertEqual(dossier.current_revision.pillars, ["增长可持续"])
        self.assertEqual(self.client.post(first_url, data).status_code, 409)
        self.assertEqual(ResearchThesisRevision.objects.filter(dossier=dossier).count(), 1)
        with self.assertRaises(ThesisRevisionConflict):
            save_first_thesis(actor=self.alice, dossier_id=dossier.pk, thesis="再来", pillars=[], questions=[])
        self.assertEqual(self.client.get(reverse("investment_research:edit", args=[dossier.pk])).status_code, 200)

    def test_private_and_viewer_cannot_write(self):
        dossier = create_exploration(actor=self.alice, security=self.security)
        detail = reverse("investment_research:detail", args=[dossier.pk])
        first = reverse("investment_research:first_thesis", args=[dossier.pk])
        self.client.force_login(self.bob.user)
        self.assertEqual(self.client.get(detail).status_code, 404)
        self.assertEqual(self.client.post(first, {"thesis": "foreign"}).status_code, 404)
        own = create_exploration(actor=self.bob, security=self.security)
        self.assertNotEqual(own.pk, dossier.pk)
        self.alice.role = FamilyMember.ROLE_VIEWER
        self.alice.save(update_fields=["role"])
        self.client.force_login(self.alice.user)
        self.assertEqual(self.client.get(detail).status_code, 200)
        self.assertEqual(self.client.post(first, {"thesis": "viewer"}).status_code, 403)
        self.assertEqual(self.client.post(reverse("investment_research:explore"), {"security": self.other_security.pk}).status_code, 403)

    def test_duplicate_service_and_invalid_first_do_not_leave_revision(self):
        dossier = create_exploration(actor=self.alice, security=self.security)
        with self.assertRaises(DuplicateDossier):
            create_exploration(actor=self.alice, security=self.security)
        self.client.force_login(self.alice.user)
        first = reverse("investment_research:first_thesis", args=[dossier.pk])
        self.assertEqual(self.client.post(first, {"thesis": "  "}).status_code, 200)
        self.assertEqual(ResearchThesisRevision.objects.count(), 0)
