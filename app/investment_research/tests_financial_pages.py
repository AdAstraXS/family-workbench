"""The new research pages are private and never fetch on GET."""
from unittest import mock

from django.urls import reverse
from django.utils import timezone

from .models import FutuFinancialSnapshot
from .tests import ResearchViewTestBase


class FinancialPageTests(ResearchViewTestBase):
    def setUp(self):
        self.member = self.make_member(self.family, "FinancialOwner")
        self.other = self.make_member(self.family, "FinancialOther")
        self.dossier = self.create_dossier_for(self.member)

    @mock.patch("investment_research.views.refresh_futu_financials")
    def test_get_pages_are_private_and_do_not_refresh_provider(self, refresh):
        financials = reverse("investment_research:financials", args=[self.dossier.pk])
        futu = reverse("investment_research:futu_financials", args=[self.dossier.pk])
        analysis = reverse("investment_research:thesis_analysis", args=[self.dossier.pk])
        self.login(self.other)
        for url in (financials, futu, analysis):
            self.assertEqual(self.client.get(url).status_code, 404)
        self.login(self.member)
        for url in (financials, futu, analysis):
            self.assertEqual(self.client.get(url).status_code, 200)
        self.assertContains(self.client.get(financials), "财务概览尚未形成")
        self.assertContains(self.client.get(futu), "还没有富途财务数据")
        refresh.assert_not_called()

    @mock.patch("investment_research.company_jobs.enqueue")
    def test_futu_refresh_requires_post_and_owner(self, refresh):
        url = reverse("investment_research:futu_financials", args=[self.dossier.pk])
        self.login(self.other)
        self.assertEqual(self.client.post(url).status_code, 404)
        refresh.assert_not_called()
        self.login(self.member)
        self.assertEqual(self.client.post(url).status_code, 302)
        refresh.assert_called_once_with(self.member, self.dossier, ["financials"])

    def test_saved_futu_statement_and_breakdown_render_without_open_d(self):
        FutuFinancialSnapshot.objects.create(
            security=self.dossier.security, provider_code="US.ACME",
            fetched_at=timezone.now(),
            data={"statements": [{"type": 1, "title": "利润表", "reports": [
                {"period": "2025/FY", "period_end": "2025-12-31", "currency": "USD",
                 "standards": "US GAAP", "auditor": "", "items": [
                     {"field_id": 5001, "name": "Total Revenue", "amount": "12000000000", "yoy": "20"},
                 ]},
            ]}], "breakdown": {"period": "2025/FY", "currency": "USD",
                               "groups": [{"type": "产品", "items": [
                                   {"name": "Cloud", "amount": "9000000000", "ratio": "75"},
                               ]}]}, "breakdown_error": ""},
        )
        self.login(self.member)
        response = self.client.get(reverse("investment_research:futu_financials",
                                           args=[self.dossier.pk]))
        self.assertContains(response, "Total Revenue")
        self.assertContains(response, "Cloud")
        self.assertContains(response, "US GAAP")
