import json
import os
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.db import connection
from django.http import Http404
from django.test import TestCase, Client, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from ai_analysis.models import AiProvider
from family_core.models import Family, FamilyMember
from portfolio.models import Security
from investment_research.services import create_dossier, save_thesis_revision
from .models import *
from .services import *
from .analysis import set_consent, analyze_candidate, validate_result
from .budget import reserve, settle, budget_status
from .collection import collect_source
from .worker import acquire, run_cycle, queue_run
from .research_bridge import append_news


class WatchTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.family = Family.objects.create(name="Watch test")
        cls.user = get_user_model().objects.create_user(
            username="watch-owner", password="local-test"
        )
        cls.member = FamilyMember.objects.create(
            user=cls.user, family=cls.family, display_name="Owner"
        )
        cls.other_user = get_user_model().objects.create_user(
            username="watch-admin", password="local-test"
        )
        cls.other = FamilyMember.objects.create(
            user=cls.other_user,
            family=cls.family,
            display_name="Admin",
            role=FamilyMember.ROLE_ADMIN,
        )
        cls.foreign = Family.objects.create(name="Other family")
        cls.security = Security.objects.create(
            symbol="MSFT", name="Microsoft", market="US", asset_type="stock"
        )
        cls.dossier = create_dossier(
            actor=cls.member,
            security=cls.security,
            initial_thesis="云业务增长值得跟踪",
            pillars=["云需求持续增长"],
            questions=[],
        )
        cls.source = NewsSource.objects.create(
            family=cls.family,
            key="test",
            name="Test source",
            url="https://example.com/feed",
            market="美国",
            enabled=True,
        )
        cls.version, _ = ingest(
            cls.source,
            external_id="one",
            title="Microsoft cloud demand grows",
            summary="Cloud demand is growing while capital spending needs checking.",
            url="https://example.com/one",
            published_at=timezone.now(),
        )
        cls.provider = AiProvider.objects.create(
            name="Test provider",
            provider_type="openai_compatible",
            model_name="test",
            base_url="https://example.ai/v1",
            extra_data={
                "allow_research_analysis": True,
                "research_policy_version": "research-document-v1",
                "research_policy_reviewed_on": "2026-09-30",
                "api_key_env_var": "WATCH_TEST_KEY",
                "research_max_input_chars": 20000,
                "research_max_output_tokens": 1500,
                "research_input_usd_per_million": "1",
                "research_output_usd_per_million": "2",
                "research_max_estimated_usd": "1",
                "watch_usd_cny": "7",
            },
        )

    def candidate(self):
        return associate(
            self.member,
            self.dossier.pk,
            self.version.pk,
            self.dossier.current_revision_id,
        )

    def test_private_dossier_has_no_admin_override(self):
        with self.assertRaises(Http404):
            associate(
                self.other,
                self.dossier.pk,
                self.version.pk,
                self.dossier.current_revision_id,
            )

    def test_versions_are_append_only_and_stale(self):
        candidate = self.candidate()
        version, created = ingest(
            self.source,
            external_id="one",
            title=self.version.title,
            summary="Updated public summary",
            url=self.version.url,
            status="corrected",
        )
        self.assertTrue(created)
        self.assertEqual(version.number, 2)
        candidate.refresh_from_db()
        self.assertTrue(candidate_stale(candidate))
        self.version.refresh_from_db()
        self.assertIn("growing", self.version.summary)
        with self.assertRaises(Conflict):
            self.candidate()

    def test_same_content_deduplicates_and_sources_remain(self):
        source = NewsSource.objects.create(
            family=self.family, key="second", name="Second", url="https://example.org"
        )
        duplicate, _ = ingest(
            source,
            external_id="copy",
            title=self.version.title,
            summary=self.version.summary,
            url="https://example.org/copy",
            published_at=self.version.published_at,
        )
        self.assertEqual(duplicate.material.event_id, self.version.material.event_id)
        self.assertEqual(duplicate.original_chain, self.version.original_chain)
        self.client.force_login(self.user)
        response = self.client.get(reverse("investment_watch:news"), {"format": "json"})
        self.assertEqual(response.json()["total"], 1)

    def test_revision_change_can_reassociate_without_rewriting_history(self):
        candidate = self.candidate()
        old = candidate.revision_id
        save_thesis_revision(
            actor=self.member,
            dossier_id=self.dossier.pk,
            expected_revision_id=old,
            thesis="新的判断",
            pillars=["资本回报需要改善"],
            questions=[],
            change_reason="新资料",
        )
        self.dossier.refresh_from_db()
        new = self.candidate()
        self.assertNotEqual(candidate.pk, new.pk)
        candidate.refresh_from_db()
        self.assertEqual(candidate.revision_id, old)
        self.assertTrue(candidate_stale(candidate))

    def test_idempotency_conflicts(self):
        call = lambda: {"id": self.candidate().pk}
        a = idempotent(self.member, "associate", "test-key-123", {"x": 1}, call)
        self.assertEqual(
            a, idempotent(self.member, "associate", "test-key-123", {"x": 1}, call)
        )
        with self.assertRaises(Conflict):
            idempotent(self.member, "associate", "test-key-123", {"x": 2}, call)
        self.assertEqual(ResearchCandidate.objects.count(), 1)

    def test_rule_pause_does_not_change_news_or_manual_candidates(self):
        rule = save_rule(
            self.member, self.dossier.pk, {"aliases": ["Microsoft"], "enabled": True}, 0
        )
        self.assertEqual(recall(self.dossier), 1)
        save_rule(
            self.member,
            self.dossier.pk,
            {"aliases": ["Microsoft"], "enabled": False},
            rule.version,
        )
        self.assertEqual(recall(self.dossier), 0)
        self.assertEqual(public_versions(self.member).count(), 1)
        self.assertEqual(ResearchCandidate.objects.count(), 1)

    def test_annotation_is_private_and_optimistic(self):
        record = annotate(
            self.member,
            self.version.material.event_id,
            {"saved": True, "read": False, "note": "private note"},
            0,
        )
        with self.assertRaises(Conflict):
            annotate(
                self.member,
                record.event_id,
                {"saved": False, "read": False, "note": "overwrite"},
                0,
            )
        self.client.force_login(self.other_user)
        self.assertNotContains(
            self.client.get(reverse("investment_watch:saved")), "private note"
        )

    def test_cross_family_material_not_visible(self):
        source = NewsSource.objects.create(
            family=self.foreign,
            key="foreign",
            name="Foreign",
            url="https://example.net",
        )
        version, _ = ingest(
            source,
            external_id="x",
            title="Private family material",
            summary="x",
            url="https://example.net/x",
        )
        with self.assertRaises(Http404):
            version_for(self.member, version.pk)
        self.client.force_login(self.user)
        self.assertEqual(
            self.client.get(
                reverse("investment_watch:news_detail", args=[version.material_id])
            ).status_code,
            404,
        )

    def test_viewer_and_csrf_block_mutations(self):
        self.member.role = FamilyMember.ROLE_VIEWER
        self.member.save()
        with self.assertRaises(PermissionDenied):
            self.candidate()
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        self.assertEqual(
            client.post(reverse("investment_watch:rules"), {}).status_code, 403
        )

    def test_get_pages_render_without_database_mutations_or_network(self):
        candidate = self.candidate()
        self.client.force_login(self.user)
        routes = [
            ("news", []),
            ("topics", []),
            ("items", []),
            ("item", [candidate.pk]),
            ("news_detail", [self.version.material_id]),
            ("rules", []),
            ("saved", []),
            ("coverage", []),
        ]
        with (
            patch(
                "investment_watch.collection.fetch_public_url",
                side_effect=AssertionError("GET network"),
            ),
            CaptureQueriesContext(connection) as queries,
        ):
            for name, args in routes:
                self.assertEqual(
                    self.client.get(
                        reverse("investment_watch:" + name, args=args)
                    ).status_code,
                    200,
                    name,
                )
        mutations = [
            q["sql"]
            for q in queries
            if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
        ]
        self.assertEqual(mutations, [])

    def test_strict_api_invalid_inputs_and_stale_revision(self):
        self.client.force_login(self.user)
        url = (
            reverse("investment_watch:associate", args=[self.version.material_id])
            + "?format=json"
        )
        body = {
            "dossier_id": self.dossier.pk,
            "material_version": self.version.pk,
            "expected_revision": 0,
        }
        self.assertEqual(
            self.client.post(
                url,
                json.dumps(body),
                content_type="application/json",
                HTTP_IDEMPOTENCY_KEY="test-key-123",
            ).status_code,
            409,
        )
        body["dossier_id"] = 1.5
        self.assertEqual(
            self.client.post(
                url,
                json.dumps(body),
                content_type="application/json",
                HTTP_IDEMPOTENCY_KEY="test-key-123",
            ).status_code,
            400,
        )

    def test_invalid_quote_cannot_be_directional(self):
        candidate = self.candidate()
        with self.assertRaises(WatchError):
            validate_result(
                json.dumps(
                    {
                        "assessments": [
                            {
                                "assumption_key": "pillar:0",
                                "direction": "support",
                                "explanation": "yes",
                                "quote": "Invented quote",
                            }
                        ]
                    }
                ),
                candidate,
            )

    def test_budget_failure_remains_reserved(self):
        receipt = reserve(self.member, self.provider, "a" * 64, Decimal(".8"))
        settle(receipt, failed=True)
        self.assertEqual(budget_status(self.family)["daily"], Decimal(".8"))
        with self.assertRaises(WatchError):
            reserve(self.member, self.provider, "b" * 64, Decimal(".3"))

    def test_overrun_is_recorded_and_stops_calls(self):
        receipt = reserve(self.member, self.provider, "a" * 64, Decimal(".1"))
        settle(receipt, Decimal(".2"))
        receipt.refresh_from_db()
        self.assertEqual(receipt.status, "overrun")
        self.assertEqual(budget_status(self.family)["daily"], Decimal(".2"))
        with self.assertRaises(WatchError):
            reserve(self.member, self.provider, "b" * 64, Decimal(".1"))

    @override_settings(INVESTMENT_WATCH_MODEL_ENABLED=True)
    def test_authorized_model_validates_and_caches(self):
        candidate = self.candidate()
        with self.assertRaises(WatchError):
            analyze_candidate(candidate.pk)
        set_consent(self.member, self.dossier.pk, self.provider, True)
        result = {
            "assessments": [
                {
                    "assumption_key": "pillar:0",
                    "direction": "support",
                    "explanation": "需求得到支持，仍需持续验证。",
                    "quote": "Cloud demand is growing",
                    "conditions": "下期保持",
                    "gaps": "缺少现金数据",
                }
            ]
        }
        response = json.dumps(
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": json.dumps(result)},
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 100},
            }
        ).encode()
        with (
            patch.dict(os.environ, {"WATCH_TEST_KEY": "test-only"}),
            patch(
                "investment_watch.analysis._chat_url",
                return_value="https://example.ai/chat",
            ),
            patch(
                "investment_watch.analysis._default_transport", return_value=response
            ) as transport,
        ):
            self.assertEqual(analyze_candidate(candidate.pk), 1)
            self.assertEqual(analyze_candidate(candidate.pk), 0)
            self.assertEqual(transport.call_count, 1)
        self.assertEqual(ThesisEvidence.objects.count(), 1)

    def test_research_packet_frozen_and_chain_deduped(self):
        candidate = self.candidate()
        candidate.selected_for_research = True
        candidate.save()
        packet = append_news({"evidence": [], "sources": []}, self.dossier)
        self.assertEqual(len(packet["news_snapshots"]), 1)
        ingest(
            self.source,
            external_id="one",
            title=self.version.title,
            summary="new",
            url=self.version.url,
        )
        self.assertIn("growing", packet["news_snapshots"][0]["excerpt"])
        self.assertEqual(
            append_news({"evidence": [], "sources": []}, self.dossier)["evidence"], []
        )

    @override_settings(INVESTMENT_WATCH_COLLECT_ENABLED=True)
    def test_rss_collection_conditional_and_repeat(self):
        xml = b"<rss><channel><item><guid>new</guid><title>Microsoft cloud news</title><link>https://example.com/new</link><description>Cloud demand</description><pubDate>Tue, 29 Sep 2026 08:00:00 GMT</pubDate></item></channel></rss>"
        from email.utils import format_datetime

        xml = xml.replace(
            b"Tue, 29 Sep 2026 08:00:00 GMT", format_datetime(timezone.now()).encode()
        )
        response = SimpleNamespace(
            body=xml, not_modified=False, etag="abc", last_modified="yesterday"
        )
        self.assertEqual(
            collect_source(self.source, fetcher=lambda *a, **k: response, force=True)[
                "added"
            ],
            1,
        )
        self.assertEqual(
            collect_source(self.source, fetcher=lambda *a, **k: response, force=True)[
                "added"
            ],
            0,
        )
        self.assertEqual(
            collect_source(self.source, fetcher=lambda *a, **k: response)["status"],
            "not_due",
        )

    def test_worker_lease_excludes_second_cycle(self):
        self.assertTrue(acquire(self.family))
        self.assertIsNone(acquire(self.family))
        self.assertEqual(run_cycle(self.family), {"status": "busy"})

    def test_worker_continues_next_batch_without_repaying_completed_inputs(self):
        from .analysis import analysis_key

        self.candidate()
        for index in range(3):
            version, _ = ingest(
                self.source,
                external_id=f"batch-{index}",
                title=f"Cloud demand development {index}",
                summary="Public article",
                url=f"https://example.com/batch-{index}",
            )
            associate(
                self.member,
                self.dossier.pk,
                version.pk,
                self.dossier.current_revision_id,
            )
        set_consent(self.member, self.dossier.pk, self.provider, True)
        run = queue_run(self.dossier)

        def analyze(pk):
            candidate = ResearchCandidate.objects.get(pk=pk)
            ThesisEvidence.objects.create(
                candidate=candidate,
                revision=candidate.revision,
                assumption_key="pillar:0",
                direction="unknown",
                explanation="Insufficient material",
                input_key=analysis_key(candidate, self.provider),
            )
            return 1

        with patch(
            "investment_watch.worker.analyze_candidate", side_effect=analyze
        ) as model:
            run_cycle(self.family, collect=False)
            run.refresh_from_db()
            self.assertEqual(run.status, "queued")
            self.assertEqual(model.call_count, 3)
            run_cycle(self.family, collect=False)
            run.refresh_from_db()
            self.assertEqual(run.status, "completed")
            self.assertEqual(model.call_count, 4)
            run_cycle(self.family, collect=False)
            self.assertEqual(model.call_count, 4)

    def test_selected_news_remains_visible_without_model_configuration(self):
        candidate = self.candidate()
        candidate.selected_for_research = True
        candidate.save()
        self.client.force_login(self.user)
        response = self.client.get(
            reverse("investment_research:thesis_analysis", args=[self.dossier.pk])
        )
        self.assertContains(response, "已选择的新闻材料")
        self.assertContains(response, "尚未配置获准处理投研资料的文本模型")

    def test_same_headline_on_different_days_is_not_same_event(self):
        from datetime import timedelta

        version, _ = ingest(
            self.source,
            external_id="other-date",
            title=self.version.title,
            summary=self.version.summary,
            url="https://example.com/next",
            published_at=self.version.published_at + timedelta(days=30),
        )
        self.assertNotEqual(version.material.event_id, self.version.material.event_id)

    def test_merge_can_be_undone_without_losing_annotations(self):
        other, _ = ingest(
            self.source,
            external_id="second",
            title="Another account of cloud demand",
            summary="Different summary",
            url="https://example.com/second",
        )
        event = self.version.material.event
        annotate(
            self.member, event.pk, {"saved": True, "read": False, "note": "retain"}, 0
        )
        organize_event(
            self.other,
            event.pk,
            other.material.event_id,
            "merge",
            "confirmed",
            event.updated_at.isoformat(),
        )
        event.refresh_from_db()
        self.assertEqual(event.merged_into_id, other.material.event_id)
        self.client.force_login(self.user)
        self.assertEqual(
            self.client.get(
                reverse("investment_watch:news"), {"format": "json"}
            ).json()["total"],
            1,
        )
        organize_event(
            self.other,
            event.pk,
            0,
            "unmerge",
            "correction",
            event.updated_at.isoformat(),
        )
        self.assertEqual(MemberAnnotation.objects.get(event=event).note, "retain")
        self.assertEqual(
            self.client.get(
                reverse("investment_watch:news"), {"format": "json"}
            ).json()["total"],
            2,
        )

    def test_cursor_is_bound_to_member_and_filters(self):
        from django.core import signing

        self.client.force_login(self.user)
        url = reverse("investment_watch:news")
        token = signing.dumps(
            {"scope": digest([self.member.pk, url, {}]), "page": 1},
            salt="investment-watch-page",
        )
        self.assertEqual(
            self.client.get(url, {"format": "json", "cursor": token}).status_code, 200
        )
        self.assertEqual(
            self.client.get(
                url, {"format": "json", "cursor": token, "q": "changed"}
            ).status_code,
            400,
        )
        self.client.force_login(self.other_user)
        self.assertEqual(
            self.client.get(url, {"format": "json", "cursor": token}).status_code, 400
        )

    def test_classification_preserves_macro_and_not_housing_as_commodity(self):
        from .catalogue import classify, qualifies

        self.assertEqual(
            classify("Federal Reserve issues FOMC statement", "", "美国")[1], "宏观"
        )
        self.assertNotEqual(classify("广州商品住房预售政策", "", "中国")[1], "商品")
        self.assertFalse(qualifies("明星私生活与恋爱八卦", ""))
        self.assertTrue(qualifies("数据中心供电面临约束", ""))

    def test_public_list_dates_are_not_scrape_dates(self):
        from .collection import parse_list

        source = SimpleNamespace(
            adapter="caixin", url="https://finance.caixin.com/", max_items=40
        )
        rows = parse_list(
            '<div><h3><a href="https://finance.caixin.com/2026-09-29/123.html">公开财经新闻标题</a></h3><p>公开目录摘要</p></div>',
            source,
        )
        self.assertEqual(rows[0]["published_precision"], "day")
        source = SimpleNamespace(
            adapter="zhitong", url="https://www.zhitongcaijing.com/", max_items=40
        )
        rows = parse_list(
            '<div><div><a href="/content/detail/123.html">公司相关新闻标题</a></div><div class="info-item-content-desc">公开摘要</div></div>',
            source,
        )
        self.assertIsNone(rows[0]["published_at"])
        self.assertEqual(rows[0]["summary"], "公开摘要")
