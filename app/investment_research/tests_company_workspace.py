"""Unified original page, read-only access and frozen incremental research."""
import json
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from ai_analysis.models import AiAnalysisRequest
from investment_watch.models import NewsSource, ResearchCandidate
from investment_watch.services import ingest, associate
from . import tests_thesis_analysis as synthesis_tests
from .research_ai import ResearchAiError
from .company_workspace import workspace_context
from .services import save_thesis_revision


class CompanyWorkspaceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        synthesis_tests.ThesisAnalysisTests.setUpTestData.__func__(cls)

    response = staticmethod(synthesis_tests.ThesisAnalysisTests.response)
    generate = synthesis_tests.ThesisAnalysisTests.generate

    def news(self, external_id):
        source, _ = NewsSource.objects.get_or_create(
            family=self.actor.family, key="workspace-news",
            defaults={"name": "Workspace news", "url": "https://example.com"})
        version, _ = ingest(source, external_id=external_id, title=f"News {external_id}",
                            summary="Demand requires verification.", url="https://example.com/news")
        candidate = associate(self.actor, self.dossier.pk, version.pk, self.dossier.current_revision_id)
        candidate.selected_for_research = True
        candidate.save()
        return candidate

    def test_original_page_and_news_entry_are_one_workspace(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("investment_research:company_research", args=[self.dossier.pk]))
        for text in ("公司研究", "持续跟踪", "资料", "我的判断", "财务与行情"):
            self.assertContains(response, text)
        self.assertRedirects(self.client.get(reverse("investment_watch:company", args=[self.dossier.pk])),
                             reverse("investment_research:company_research", args=[self.dossier.pk]))

    def test_reading_all_three_views_does_not_write_or_call_model(self):
        self.news("pending")
        self.client.force_login(self.user)
        for view in ("conclusion", "changes", "evidence"):
            with CaptureQueriesContext(connection) as queries:
                response = self.client.get(reverse("investment_research:company_research", args=[self.dossier.pk]), {"view": view}, follow=True)
            self.assertEqual(response.status_code, 200)
            self.assertFalse(any(query["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for query in queries))

    def test_private_workspace_has_no_administrator_bypass(self):
        self.client.force_login(self.other_user)
        self.assertEqual(self.client.get(reverse("investment_research:company_research", args=[self.dossier.pk])).status_code, 404)

    def test_original_judgment_and_company_list_keep_original_structure(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("investment_research:detail", args=[self.dossier.pk]))
        self.assertTemplateUsed(response, "investment_research/detail.html")
        for text in ("公司研究", "个人判断", "持续跟踪", "资料", "财务与行情"):
            self.assertContains(response, text)
        self.assertContains(response, self.dossier.current_revision.thesis)
        self.assertNotContains(response, 'css/company-research.css')
        listing = self.client.get(reverse("investment_research:index"))
        for text in ("公司", "研究进度", "观察状态"):
            self.assertContains(listing, text)
        self.assertContains(listing, reverse("investment_research:company_research", args=[self.dossier.pk]))

    def test_evidence_filter_respects_citations_and_type(self):
        self.generate()
        self.news("not-analyzed")
        self.client.force_login(self.user)
        response = self.client.get(reverse("investment_research:company_research", args=[self.dossier.pk]),
                                   {"view": "evidence", "source_type": "official", "assumption": "pillar:0"})
        self.assertEqual(len(response.context["official_rows"]), 1)
        self.assertEqual(response.context["news_rows"], [])

    def test_incremental_without_same_revision_baseline_is_rejected(self):
        with self.assertRaisesMessage(ResearchAiError, "请先完整重评"):
            self.generate(review_mode="incremental")

    def test_successful_report_survives_more_than_fifty_failed_attempts(self):
        report = self.generate()
        AiAnalysisRequest.objects.bulk_create([
            AiAnalysisRequest(family=self.actor.family, member=self.actor,
                              provider=self.provider, module="investment_research",
                              analysis_type="thesis_synthesis", status="failed",
                              scope={"dossier_id": self.dossier.pk}) for _ in range(51)])
        self.assertEqual(workspace_context(self.dossier, {})["research_report"].pk, report.pk)

    def test_search_filters_before_limiting_recent_news(self):
        target = self.news("needle")
        for index in range(101):
            self.news(f"recent-{index}")
        rows = workspace_context(self.dossier, {"view": "evidence", "q": "needle"})["news_rows"]
        self.assertEqual([row["candidate"].pk for row in rows], [target.pk])

    def test_old_report_does_not_assign_evidence_to_a_new_hypothesis(self):
        self.generate()
        save_thesis_revision(actor=self.actor, dossier_id=self.dossier.pk,
                             expected_revision_id=self.dossier.current_revision_id,
                             thesis="Changed thesis", pillars=["Different hypothesis"],
                             questions=[], change_reason="New evidence")
        self.dossier.refresh_from_db()
        context = workspace_context(self.dossier, {"view": "evidence", "assumption": "pillar:0"})
        self.assertFalse(context["report_is_current"])
        self.assertEqual(context["official_rows"], [])
        with self.assertRaisesMessage(ResearchAiError, "请先完整重评"):
            self.generate(review_mode="incremental")

    def test_incremental_without_new_material_is_rejected(self):
        self.generate()
        with self.assertRaisesMessage(ResearchAiError, "没有尚未采用"):
            self.generate(review_mode="incremental")

    @override_settings(INVESTMENT_WATCH_MODEL_ENABLED=True)
    def test_incremental_retains_baseline_and_does_not_repeat_news(self):
        self.provider.extra_data["watch_usd_cny"] = "7"
        self.provider.save()
        old = self.news("old")
        baseline = self.generate(include_news=True)
        original_scope = json.loads(json.dumps(baseline.scope))
        new = self.news("new")
        prompts = []
        def transport(request, **kwargs):
            prompts.append(json.loads(request.data)["messages"][1]["content"])
            return self.response()
        report = self.generate(include_news=True, review_mode="incremental", transport=transport)
        self.assertEqual([item["version_id"] for item in report.scope["news_snapshots"]], [new.material_version_id])
        self.assertEqual(report.scope["baseline_context"]["analysis_id"], baseline.pk)
        self.assertEqual(report.scope["baseline_news_snapshots"][0]["version_id"], old.material_version_id)
        self.assertIn("冻结的上一版研究背景", prompts[0])
        baseline.refresh_from_db()
        self.assertEqual(baseline.scope, original_scope)
        self.client.force_login(self.user)
        response = self.client.get(reverse("investment_research:company_research", args=[self.dossier.pk]),
                                   {"view": "changes"}, follow=True)
        self.assertEqual(response.context["pending_news_count"], 0)

    def test_material_selection_returns_to_original_company_page(self):
        candidate = self.news("select")
        self.client.force_login(self.user)
        response = self.client.post(reverse("investment_watch:select_research", args=[candidate.pk]),
                                    {"return_to": "company"})
        self.assertRedirects(response, reverse("investment_research:company_research", args=[self.dossier.pk]) + "?view=changes", target_status_code=302)
        self.assertFalse(ResearchCandidate.objects.get(pk=candidate.pk).selected_for_research)
