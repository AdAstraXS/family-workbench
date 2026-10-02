import gzip
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from . import tests as legacy
from . import test_body_pipeline as pipeline
from .test_body_pipeline import response, scrape_response
from .models import BodySnapshot, BodyAttempt, CandidateScreening, ResearchCandidate, WatchRule, OperationReceipt, WatchRun
from .services import ingest, WatchError, digest, save_rule
from .screening import screen_candidates, validate_screening, ready_candidates
from .source_templates import parse_source
from .research_bridge import append_news
from .profiles import profile, official
from .capture_account import refresh_usage, check_credits


@override_settings(INVESTMENT_WATCH_MODEL_ENABLED=True, INVESTMENT_WATCH_BODY_ENABLED=True)
class StandardizationTests(TestCase):
    setUpTestData = classmethod(legacy.WatchTests.setUpTestData.__func__)
    setUp = pipeline.BodyPipelineTests.setUp
    make_candidate = pipeline.BodyPipelineTests.make_candidate
    select = pipeline.BodyPipelineTests.select
    fetch = pipeline.BodyPipelineTests.fetch

    def rss(self, contents, *, description="Short summary."):
        return (f'<rss xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel><item><guid>rss-one</guid>'
                f'<title>Microsoft launches major Copilot application</title><link>https://example.com/rss-one</link>'
                f'<description><![CDATA[{description}]]></description>'
                f'<content:encoded><![CDATA[{contents}]]></content:encoded></item></channel></rss>').encode()

    def rss_ingest(self, contents):
        self.source.adapter = "rss"
        row = parse_source(self.rss(contents), self.source)[0]
        return ingest(self.source, **row)[0]

    def test_rss_content_reused_without_external_attempt(self):
        v = self.rss_ingest('<p>Microsoft launches a new enterprise application with new pricing.</p>' * 9)
        snapshot = BodySnapshot.objects.get(material_version=v)
        self.assertEqual(snapshot.method, "rss-content")
        self.assertIn(b"<p>", gzip.decompress(bytes(snapshot.raw_gzip)))
        from .services import associate
        c = associate(self.member, self.dossier.pk, v.pk, self.dossier.current_revision_id)
        self.select(c)
        transport = Mock()
        self.assertEqual(self.fetch(c, transport).pk, snapshot.pk)
        transport.assert_not_called()
        self.assertFalse(BodyAttempt.objects.exists())

    def test_description_or_truncated_content_not_promoted_to_body(self):
        self.source.adapter = "rss"
        row = parse_source(self.rss("", description="Microsoft announcement. " * 100), self.source)[0]
        v, _ = ingest(self.source, **row)
        self.assertFalse(BodySnapshot.objects.filter(material_version=v).exists())
        v = self.rss_ingest("Microsoft announced capabilities. " * 15 + " Continue reading")
        self.assertFalse(BodySnapshot.objects.filter(material_version=v).exists())

    def test_changed_feed_body_creates_new_version_without_overwriting_old(self):
        old = self.rss_ingest("Original product capabilities. " * 20)
        new = self.rss_ingest("Updated product capabilities and pricing. " * 20)
        self.assertNotEqual(old.pk, new.pk)
        self.assertNotEqual(old.content_hash, new.content_hash)
        self.assertIn("Original", BodySnapshot.objects.get(material_version=old).text)

    def test_atom_xhtml_content_is_saved(self):
        body = ('<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>atom-one</id>'
                '<title>Microsoft product release</title><link href="https://example.com/atom"/>'
                '<summary>A short summary.</summary><content type="xhtml"><div xmlns="http://www.w3.org/1999/xhtml">'
                + '<p>Commercial product capabilities need careful checking.</p>' * 20
                + '</div></content></entry></feed>').encode()
        self.source.adapter = "rss"
        row = parse_source(body, self.source)[0]
        version, _ = ingest(self.source, **row)
        self.assertIn("Commercial product", BodySnapshot.objects.get(material_version=version).text)

    def test_company_assessment_freezes_body_and_precise_hash(self):
        c = self.make_candidate(1)
        self.select(c)
        snapshot = self.fetch(c, lambda *a, **k: scrape_response("Exact saved body. " * 200))
        c.selected_for_research = True
        c.save()
        packet = append_news({"sources": [], "evidence": []}, self.dossier)
        news = packet["news_snapshots"][0]
        citation = packet["evidence"][0]["citations"][0]
        self.assertEqual(news["body_id"], snapshot.pk)
        self.assertGreater(len(news["excerpt"]), 1500)
        self.assertEqual(citation["hash"], digest(news["excerpt"]))
        self.assertEqual(citation["end"], len(news["excerpt"]))
        self.assertEqual(packet["news_snapshots"][0]["excerpt"], news["excerpt"])

    def test_cross_batch_duplicate_receives_no_body_slot(self):
        original = self.make_candidate(1)
        self.select(original)
        duplicate = self.make_candidate(2)
        def transport(request, **kwargs):
            sent = json.loads(json.loads(request.data)["messages"][1]["content"])
            self.assertEqual(sent["recent_selected"][0]["candidate_id"], original.pk)
            return response({"decisions": [{"candidate_id": duplicate.pk, "selected": False,
                "priority": 0, "relevance": "direct", "duplicate_of": original.pk, "reason": "同一产品发布，没有新增信息。"}]})
        screen_candidates(self.dossier, [duplicate], transport=transport, url_validator=lambda p: "https://example.ai/chat")
        self.assertEqual(CandidateScreening.objects.get(candidate=duplicate).duplicate_of_id, original.pk)
        self.assertNotIn(duplicate.pk, [c.pk for c in ready_candidates(self.dossier)])
        self.assertFalse(BodyAttempt.objects.exists())

    def test_followup_is_independently_selected(self):
        original = self.make_candidate(1)
        self.select(original)
        followup = self.make_candidate(2)
        result = {"decisions": [{"candidate_id": followup.pk, "selected": True, "priority": 95,
                  "relevance": "direct", "duplicate_of": None, "reason": "新增定价与客户订单，需读正文。"}]}
        screen_candidates(self.dossier, [followup], transport=lambda *a, **k: response(result), url_validator=lambda p: "https://example.ai/chat")
        self.assertIn(followup.pk, [c.pk for c in ready_candidates(self.dossier)])

    def test_duplicate_reference_cannot_escape_supplied_private_context(self):
        c = self.make_candidate(1)
        for reference in (c.pk, c.pk + 900):
            with self.assertRaises(WatchError):
                validate_screening(json.dumps({"decisions": [{"candidate_id": c.pk, "selected": False,
                    "priority": 0, "reason": "Duplicate", "duplicate_of": reference}]}), [c])

    def test_company_profile_and_official_host_are_configurable(self):
        rule = WatchRule.objects.get(dossier=self.dossier)
        save_rule(self.member, self.dossier.pk, {"enabled": True, "aliases": ["NVIDIA"],
            "products": ["Blackwell"], "official_domains": ["nvidia.com"], "business_context": "GPU与数据中心"}, rule.version)
        company = profile(self.dossier)
        self.assertTrue(official(SimpleNamespace(url="https://blogs.nvidia.com/a", official_version_id=None), company))
        self.assertFalse(official(SimpleNamespace(url="https://nvidia.com.evil.example/a", official_version_id=None), company))
        self.assertEqual(company["products"], ["Blackwell"])
        from .catalogue import match_rule
        rule.refresh_from_db()
        self.assertTrue(match_rule(rule, SimpleNamespace(title="Blackwell major launch", summary=""))[0])
        self.assertTrue(match_rule(rule, SimpleNamespace(title="New product codename", summary="", url="https://blogs.nvidia.com/a"))[0])

    def test_manual_request_is_owned_idempotent_and_still_capped(self):
        c = self.make_candidate(1)
        self.select(c, selected=False)
        url = reverse("investment_watch:request_reading", args=[c.pk])
        data = {"reason": "重大产品升级", "expected_revision": c.revision_id, "idempotency_key": "manual-reading-123"}
        self.client.force_login(self.other_user)
        self.assertEqual(self.client.post(url, data).status_code, 404)
        self.client.force_login(self.user)
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.assertEqual(OperationReceipt.objects.filter(operation="request-reading").count(), 1)
        c.refresh_from_db()
        self.assertTrue(c.reading_requested)
        self.assertIn(c.pk, [x.pk for x in ready_candidates(self.dossier)])
        for i in range(3):
            other = self.make_candidate(10 + i)
            self.select(other)
            self.fetch(other)
        with self.assertRaisesMessage(WatchError, "3 篇"):
            self.fetch(c)

    def test_important_default_keeps_rejected_items_in_history(self):
        selected = self.make_candidate(1)
        rejected = self.make_candidate(2)
        self.select(selected)
        self.select(rejected, selected=False)
        self.client.force_login(self.user)
        page = self.client.get(reverse("investment_watch:items"))
        self.assertContains(page, selected.material_version.title)
        self.assertNotContains(page, rejected.material_version.title)
        self.assertContains(self.client.get(reverse("investment_watch:items"), {"scope": "all"}), rejected.material_version.title)

    def test_shared_credit_observation_is_cached_and_zero_stops_new_capture(self):
        transport = Mock(return_value=json.dumps({"success": True, "data": {
            "remainingCredits": 0, "planCredits": 1000, "billingPeriodEnd": "2026-12-31T00:00:00Z"}}).encode())
        refresh_usage(transport=transport)
        refresh_usage(transport=transport)
        self.assertEqual(transport.call_count, 1)
        with self.assertRaisesMessage(WatchError, "额度已耗尽"):
            check_credits()
        c = self.make_candidate(1)
        self.select(c)
        capture = Mock()
        with self.assertRaises(WatchError):
            self.fetch(c, capture)
        capture.assert_not_called()
        self.assertFalse(BodyAttempt.objects.exists())

    def test_get_does_not_query_external_credit_api(self):
        self.client.force_login(self.user)
        with patch("investment_watch.capture_account.refresh_usage") as refresh:
            self.assertEqual(self.client.get(reverse("investment_watch:coverage")).status_code, 200)
            refresh.assert_not_called()

    def test_msft_rollout_is_idempotent_and_does_not_enable_other_company_ai(self):
        from importlib import import_module
        from django.apps import apps
        from .collection import seed_sources
        from .models import NewsSource, WatchConsent
        self.source.enabled = False
        self.source.save()
        seed_sources(self.family)
        NewsSource.objects.filter(family=self.family, key__in=["wallstreetcn", "zhitong", "caixin-finance", "microsoft-blog"]).update(enabled=True)
        configure = import_module("investment_watch.migrations.0010_msft_product_sources").configure
        configure(apps, None)
        configure(apps, None)
        self.assertEqual(NewsSource.objects.filter(family=self.family, enabled=True).count(), 5)
        self.assertFalse(NewsSource.objects.get(family=self.family, key="caixin-finance").enabled)
        self.assertEqual(WatchConsent.objects.filter(active=True).count(), 1)
        self.assertEqual(WatchRule.objects.get(dossier=self.dossier).official_domains, ["microsoft.com"])

    def test_source_failure_count_does_not_advance_conditional_cursor(self):
        from .collection import collect_source
        from intelligence.http_client import SafeHttpError
        with override_settings(INVESTMENT_WATCH_COLLECT_ENABLED=True):
            self.source.cursor = {"etag": "known-version"}
            self.source.save()
            collect_source(self.source, fetcher=Mock(side_effect=SafeHttpError("network", "连接失败")), force=True)
        self.source.refresh_from_db()
        self.assertEqual(self.source.cursor, {"etag": "known-version"})
        self.assertEqual(self.source.consecutive_failures, 1)
