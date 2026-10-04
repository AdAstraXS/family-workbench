import gzip
import hashlib
import json
from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import Mock, patch

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import tests as legacy
from .analysis import analyze_candidate, analysis_key, validate_result, set_consent
from .body_capture import capture_body, reserve_body, china_day, MAX_RESPONSE_BYTES, firecrawl_key
from .models import BodyAttempt, BodySnapshot, BudgetReceipt, CandidateScreening, ScreeningBatch, WatchPipelineState, WatchRule, ThesisEvidence, NewsSource
from .screening import screen_candidates, screening_key, validate_screening
from .services import ingest, associate, WatchError, digest
from .worker import process_pipeline, run_cycle

BODY_TEXT = "Microsoft reports that cloud demand is growing. Customers are adopting Azure for production workloads. " * 4


def response(content):
    return json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(content)}}],
                       "usage": {"prompt_tokens": 100, "completion_tokens": 80}}).encode()


def scrape_response(text=BODY_TEXT, **metadata):
    return json.dumps({"success": True, "data": {"markdown": text, "metadata": {"statusCode": 200, **metadata}}}).encode()


@override_settings(INVESTMENT_WATCH_MODEL_ENABLED=True, INVESTMENT_WATCH_BODY_ENABLED=True)
class BodyPipelineTests(TestCase):
    setUpTestData = classmethod(legacy.WatchTests.setUpTestData.__func__)
    candidate = legacy.WatchTests.candidate

    def setUp(self):
        self.env = patch.dict("os.environ", {"WATCH_TEST_KEY": "local-test", "WATCH_TEST_FIRECRAWL_KEY": "local-test"})
        self.env.start()
        self.addCleanup(self.env.stop)
        set_consent(self.member, self.dossier.pk, self.provider, True)
        WatchRule.objects.create(dossier=self.dossier, aliases=["Microsoft", "Copilot", "Azure"])
        WatchPipelineState.objects.create(family=self.family, started_at=timezone.now() - timedelta(minutes=1))

    def make_candidate(self, index):
        version, _ = ingest(self.source, external_id=f"body-{index}", title=f"Microsoft announces major Copilot capabilities {index}",
            summary="Copilot adds autonomous application capabilities; commercial impact needs checking.",
            url=f"https://example.com/body-{index}", published_at=timezone.now())
        return associate(self.member, self.dossier.pk, version.pk, self.dossier.current_revision_id)

    @override_settings(INVESTMENT_WATCH_FIRECRAWL_KEY_ENV="WATCH_SHARED_TEST_KEY", KNOWLEDGE_FIRECRAWL_API_KEY="shared-fixture")
    def test_existing_knowledge_key_is_reused_and_explicit_key_wins(self):
        with patch.dict("os.environ", {"WATCH_SHARED_TEST_KEY": ""}):
            self.assertEqual(firecrawl_key(), "shared-fixture")
        with patch.dict("os.environ", {"WATCH_SHARED_TEST_KEY": "dedicated-fixture"}):
            self.assertEqual(firecrawl_key(), "dedicated-fixture")

    def select(self, candidate, priority=90, selected=True):
        key = screening_key(candidate, self.provider)
        batch = ScreeningBatch.objects.create(dossier=self.dossier, input_key=digest(["batch", key]), status="completed")
        return CandidateScreening.objects.create(candidate=candidate, batch=batch, input_key=key,
            selected=selected, priority=priority, reason="重大产品升级，正文需核对能力和商用范围。")

    def fetch(self, candidate, transport=None):
        return capture_body(candidate, transport=transport or (lambda *a, **k: scrape_response()), url_validator=lambda u: u)

    def test_batch_screening_charges_once_and_does_not_fill_a_quota(self):
        candidates = [self.make_candidate(i) for i in range(3)]
        transport = Mock(return_value=response({"decisions": [{"candidate_id": c.pk, "selected": False,
            "priority": 0, "reason": "没有重要新事件。"} for c in candidates]}))
        self.assertEqual(screen_candidates(self.dossier, candidates, transport=transport, url_validator=lambda p: "https://example.ai/v1/chat/completions"), 3)
        self.assertEqual(screen_candidates(self.dossier, candidates, transport=transport), 0)
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(BudgetReceipt.objects.count(), 1)
        self.assertEqual(BodyAttempt.objects.count(), 0)
        self.assertFalse(CandidateScreening.objects.filter(selected=True).exists())

    def test_product_launch_is_available_to_ai_without_revenue_requirement(self):
        c = self.make_candidate(1)
        def transport(request, **kwargs):
            payload = json.loads(request.data)
            self.assertIn("重要产品发布", payload["messages"][0]["content"])
            self.assertIn("不得因缺少实际收入数字", payload["messages"][0]["content"])
            sent = json.loads(payload["messages"][1]["content"])
            self.assertEqual(sent["candidates"][0]["candidate_id"], c.pk)
            return response({"decisions": [{"candidate_id": c.pk, "selected": True, "priority": 95, "reason": "Copilot 重大升级，需验证可用范围。"}]})
        screen_candidates(self.dossier, [c], transport=transport, url_validator=lambda p: "https://example.ai/v1/chat/completions")
        self.assertTrue(CandidateScreening.objects.get(candidate=c).selected)

    def test_malformed_or_interrupted_screen_does_not_retry_or_fetch(self):
        c = self.make_candidate(1)
        transport = Mock(return_value=response({"decisions": [{"candidate_id": c.pk + 999, "selected": True, "priority": 95, "reason": "wrong id"}]}))
        with self.assertRaises(WatchError):
            screen_candidates(self.dossier, [c], transport=transport, url_validator=lambda p: "https://example.ai/v1/chat/completions")
        self.assertEqual(screen_candidates(self.dossier, [c], transport=transport), 0)
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(ScreeningBatch.objects.get().status, "failed")
        with self.assertRaises(WatchError):
            self.fetch(c)
        self.assertFalse(BodyAttempt.objects.exists())

    def test_screen_requires_exact_coverage_boolean_and_bounded_priority(self):
        c = self.make_candidate(1)
        for item in ({"candidate_id": c.pk, "selected": "true", "priority": 1, "reason": "x"},
                     {"candidate_id": c.pk, "selected": True, "priority": 101, "reason": "x"}):
            with self.assertRaises(WatchError):
                validate_screening(json.dumps({"decisions": [item]}), [c])

    def test_no_historical_backlog_is_screened_automatically(self):
        c = self.candidate()
        c.manual = False
        c.save()
        WatchPipelineState.objects.filter(family=self.family).update(started_at=timezone.now() + timedelta(minutes=1))
        transport = Mock()
        self.assertEqual(screen_candidates(self.dossier, [c], transport=transport), 0)
        transport.assert_not_called()

    def test_current_rules_are_rechecked_before_initial_screen(self):
        c = self.candidate()
        c.manual = False
        c.save()
        WatchRule.objects.filter(dossier=self.dossier).update(aliases=["Unrelated company"])
        transport = Mock()
        self.assertEqual(screen_candidates(self.dossier, [c], transport=transport), 0)
        transport.assert_not_called()

    def test_capture_uses_only_single_page_and_keeps_source_snapshot(self):
        c = self.make_candidate(1)
        self.select(c)
        def transport(request, timeout):
            self.assertEqual(request.full_url, "https://api.firecrawl.dev/v2/scrape")
            payload = json.loads(request.data)
            self.assertEqual(payload["formats"], ["markdown"])
            self.assertTrue(payload["onlyMainContent"])
            self.assertFalse(payload["onlyCleanContent"])
            self.assertEqual(payload["parsers"], [])
            self.assertEqual(BodyAttempt.objects.get().status, "reserved")
            return scrape_response()
        snapshot = self.fetch(c, transport)
        self.assertEqual(snapshot.text, BODY_TEXT)
        self.assertEqual(snapshot.content_hash, hashlib.sha256(BODY_TEXT.encode()).hexdigest())
        self.assertEqual(json.loads(gzip.decompress(bytes(snapshot.raw_gzip)))["data"]["markdown"], BODY_TEXT)
        self.assertEqual(BodyAttempt.objects.get().status, "completed")
        repeated = Mock()
        self.assertEqual(self.fetch(c, repeated).pk, snapshot.pk)
        repeated.assert_not_called()

    def test_failed_capture_consumes_slot_and_never_silently_retries(self):
        c = self.make_candidate(1)
        self.select(c)
        transport = Mock(side_effect=TimeoutError("do not expose transport details"))
        for _ in range(2):
            with self.assertRaises(WatchError):
                self.fetch(c, transport)
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(BodyAttempt.objects.get().status, "failed")
        self.assertFalse(BodySnapshot.objects.exists())

    def test_daily_company_cap_includes_failed_calls_across_sources_and_owners(self):
        for i in range(3):
            c = self.make_candidate(i)
            self.select(c)
            if i == 0:
                with self.assertRaises(WatchError):
                    self.fetch(c, Mock(side_effect=TimeoutError()))
            else:
                self.fetch(c)
        c = self.make_candidate(4)
        self.select(c)
        transport = Mock()
        with self.assertRaisesMessage(WatchError, "3 篇"):
            self.fetch(c, transport)
        transport.assert_not_called()
        self.assertEqual(BodyAttempt.objects.count(), 3)

    def test_quota_resets_at_beijing_midnight(self):
        before = datetime(2026, 10, 2, 15, 59, tzinfo=dt_timezone.utc)
        with patch("investment_watch.body_capture.timezone.now", return_value=before):
            self.assertEqual(str(china_day()), "2026-10-02")
            for i in range(3):
                reserve_body(self.make_candidate(i))
        after = before + timedelta(minutes=2)
        with patch("investment_watch.body_capture.timezone.now", return_value=after):
            self.assertEqual(str(china_day()), "2026-10-03")
            reserve_body(self.make_candidate(4))
        self.assertEqual(BodyAttempt.objects.count(), 4)

    def test_other_owner_and_source_use_same_company_cap(self):
        from investment_research.services import create_dossier
        for i in range(3):
            reserve_body(self.make_candidate(i))
        other_dossier = create_dossier(actor=self.other, security=self.security,
            initial_thesis="Other owner's thesis", pillars=["Demand"], questions=[])
        source = NewsSource.objects.create(family=self.family, key="second-body-source", name="Another source", url="https://example.org/feed")
        version, _ = ingest(source, external_id="other", title="Microsoft major contract", summary="Different source and owner",
            url="https://example.org/contract", published_at=timezone.now())
        candidate = associate(self.other, other_dossier.pk, version.pk, other_dossier.current_revision_id)
        with self.assertRaisesMessage(WatchError, "3 篇"):
            reserve_body(candidate)
        self.assertEqual(BodyAttempt.objects.count(), 3)

    def test_changed_revision_during_screen_keeps_result_as_stale(self):
        from investment_research.services import save_thesis_revision
        c = self.make_candidate(1)
        def transport(request, **kwargs):
            save_thesis_revision(actor=self.member, dossier_id=self.dossier.pk,
                expected_revision_id=self.dossier.current_revision_id, thesis="Updated thesis",
                pillars=["Updated demand"], questions=[], change_reason="Test concurrent edit")
            return response({"decisions": [{"candidate_id": c.pk, "selected": True, "priority": 95, "reason": "重大产品更新。"}]})
        screen_candidates(self.dossier, [c], transport=transport, url_validator=lambda p: "https://example.ai/v1/chat/completions")
        self.assertEqual(ScreeningBatch.objects.get().status, "stale")
        self.assertFalse(CandidateScreening.objects.get().selected)
        self.assertFalse(BodyAttempt.objects.exists())

    def test_screen_and_body_analysis_share_model_budget(self):
        c = self.make_candidate(1)
        screen_candidates(self.dossier, [c], transport=lambda *a, **k: response({"decisions": [{
            "candidate_id": c.pk, "selected": True, "priority": 95, "reason": "重大产品更新。"}]}), url_validator=lambda p: "https://example.ai/v1/chat/completions")
        snapshot = self.fetch(c)
        analyze_candidate(c.pk, transport=lambda *a, **k: response({"assessments": [{
            "assumption_key": "pillar:0", "direction": "unknown", "explanation": "缺少财务验证。", "quote": ""}]}), url_validator=lambda p: "https://example.ai/v1/chat/completions")
        self.assertEqual(BudgetReceipt.objects.count(), 2)
        self.assertTrue(all(r.member_id == self.member.pk and r.family_id == self.family.pk for r in BudgetReceipt.objects.all()))

    def test_long_body_is_saved_without_silent_truncation_or_model_charge(self):
        c = self.make_candidate(1)
        self.select(c)
        text = BODY_TEXT * 100
        snapshot = self.fetch(c, Mock(return_value=scrape_response(text)))
        self.assertEqual(snapshot.text, text)
        transport = Mock()
        with self.assertRaisesMessage(WatchError, "正文已保存"):
            analyze_candidate(c.pk, transport=transport, url_validator=lambda p: "https://example.ai/v1/chat/completions")
        transport.assert_not_called()
        self.assertFalse(BudgetReceipt.objects.exists())

    def test_no_new_body_request_when_model_budget_is_full(self):
        from .budget import reserve
        c = self.make_candidate(1)
        self.select(c)
        reserve(self.member, self.provider, "b" * 64, "1")
        transport = Mock()
        with self.assertRaisesMessage(WatchError, "模型额度不足"):
            self.fetch(c, transport)
        transport.assert_not_called()
        self.assertFalse(BodyAttempt.objects.exists())

    def test_capture_refreshes_revision_before_using_preselected_candidate(self):
        from investment_research.services import save_thesis_revision
        c = self.make_candidate(1)
        self.select(c)
        save_thesis_revision(actor=self.member, dossier_id=self.dossier.pk,
            expected_revision_id=self.dossier.current_revision_id, thesis="Updated thesis",
            pillars=["Updated demand"], questions=[], change_reason="Test edit")
        transport = Mock()
        with self.assertRaisesMessage(WatchError, "当前版本"):
            self.fetch(c, transport)
        transport.assert_not_called()
        self.assertFalse(BodyAttempt.objects.exists())

    def test_missing_key_and_unselected_candidate_make_no_requests(self):
        c = self.make_candidate(1)
        transport = Mock()
        with self.assertRaises(WatchError):
            self.fetch(c, transport)
        self.select(c)
        with patch.dict("os.environ", {"WATCH_TEST_FIRECRAWL_KEY": ""}):
            with self.assertRaisesMessage(WatchError, "密钥"):
                self.fetch(c, transport)
        transport.assert_not_called()
        self.assertFalse(BodyAttempt.objects.exists())

    def test_paywall_short_empty_and_oversize_bodies_do_not_become_evidence(self):
        for index, raw in enumerate((scrape_response("short"), scrape_response("subscribe to continue " * 8), b"x" * (MAX_RESPONSE_BYTES + 1))):
            c = self.make_candidate(index)
            self.select(c)
            with self.assertRaises(WatchError):
                self.fetch(c, Mock(return_value=raw))
        self.assertEqual(BodyAttempt.objects.filter(status="failed").count(), 3)
        self.assertFalse(BodySnapshot.objects.exists())

    def test_full_body_quotes_validated_against_exact_analyzed_input(self):
        c = self.make_candidate(1)
        self.select(c)
        before = analysis_key(c, self.provider)
        snapshot = self.fetch(c)
        self.assertNotEqual(analysis_key(c, self.provider), before)
        result = {"assessments": [{"assumption_key": "pillar:0", "direction": "support",
            "explanation": "客户采用可能支持云需求。", "quote": "Customers are adopting Azure for production workloads.",
            "conditions": "采用转化为持续用量。", "gaps": "缺少收入验证。"}]}
        with self.assertRaises(WatchError):
            validate_result(json.dumps(result), c)
        rows = validate_result(json.dumps(result), c, snapshot)
        text = c.material_version.title + "\n" + snapshot.text
        _, start, end = rows[0]["locator"].split(":")
        self.assertEqual(text[int(start):int(end)], result["assessments"][0]["quote"])
        def transport(request, **kwargs):
            sent = json.loads(json.loads(request.data)["messages"][1]["content"])
            self.assertEqual(sent["excerpt"], snapshot.text)
            self.assertEqual(sent["input_kind"], "captured_body")
            return response(result)
        self.assertEqual(analyze_candidate(c.pk, transport=transport, url_validator=lambda p: "https://example.ai/v1/chat/completions"), 1)
        self.assertEqual(ThesisEvidence.objects.get().input_body_id, snapshot.pk)

    def test_summary_cannot_bypass_body_pipeline(self):
        c = self.make_candidate(1)
        transport = Mock()
        with self.assertRaisesMessage(WatchError, "取得可用正文"):
            analyze_candidate(c.pk, transport=transport)
        transport.assert_not_called()

    def test_revoked_consent_stops_new_capture(self):
        c = self.make_candidate(1)
        self.select(c)
        set_consent(self.member, self.dossier.pk, self.provider, False)
        transport = Mock()
        with self.assertRaises(WatchError):
            self.fetch(c, transport)
        transport.assert_not_called()

    def test_duplicate_url_and_content_reuses_snapshot_without_new_slot(self):
        c = self.make_candidate(1)
        self.select(c)
        original = self.fetch(c)
        v = c.material_version
        duplicate, _ = ingest(self.source, external_id="same-url", title=v.title, summary=v.summary,
            url=v.url, published_at=v.published_at)
        other = associate(self.member, self.dossier.pk, duplicate.pk, self.dossier.current_revision_id)
        self.select(other)
        transport = Mock()
        self.assertEqual(self.fetch(other, transport).text, original.text)
        transport.assert_not_called()
        self.assertEqual(BodyAttempt.objects.count(), 1)

    def test_pipeline_selects_highest_priorities_and_caps_body_requests(self):
        candidates = [self.make_candidate(i) for i in range(5)]
        for i, c in enumerate(candidates):
            self.select(c, 70 + i)
        read = []
        def capture(request, **kwargs):
            read.append(json.loads(request.data)["url"])
            return scrape_response()
        model = Mock(return_value=response({"assessments": [{"assumption_key": "pillar:0", "direction": "unknown",
            "explanation": "缺少同口径验证。", "quote": "", "conditions": "", "gaps": "待验证。"}]}))
        with patch("investment_watch.body_capture.capture_transport", side_effect=capture), patch("investment_watch.body_capture.validate_public_http_url", side_effect=lambda u: u), patch("investment_watch.analysis._default_transport", model), patch("investment_watch.analysis._chat_url", return_value="https://example.ai/v1/chat/completions"):
            count, message, errors = process_pipeline(self.dossier)
            self.assertEqual(count, 3)
            self.assertEqual(read, [c.material_version.url for c in reversed(candidates[-3:])])
            self.assertFalse(errors)
            process_pipeline(self.dossier)
        self.assertEqual(len(read), 3)
        self.assertEqual(model.call_count, 3)

    def test_get_pages_do_not_capture_or_screen_and_keep_private_results_private(self):
        c = self.make_candidate(1)
        self.select(c)
        self.fetch(c)
        self.client.force_login(self.user)
        with patch("investment_watch.screening.screen_candidates") as screen, patch("investment_watch.body_capture.capture_transport") as capture:
            for url in (reverse("investment_watch:items"), reverse("investment_watch:item", args=[c.pk]), reverse("investment_watch:news_detail", args=[c.material_version.material_id]), reverse("investment_watch:coverage")):
                self.assertEqual(self.client.get(url).status_code, 200)
            screen.assert_not_called()
            capture.assert_not_called()
        self.client.force_login(self.other_user)
        self.assertEqual(self.client.get(reverse("investment_watch:item", args=[c.pk])).status_code, 404)
        self.assertNotContains(self.client.get(reverse("investment_watch:coverage")), "重大产品升级，正文需核对")

    def test_all_visible_batches_are_screened_before_body_slots_are_used(self):
        candidates = [self.make_candidate(i) for i in range(8)]
        priorities = {c.pk: 100 - i for i, c in enumerate(candidates)}
        screened = []
        read = []
        def screen(request, **kwargs):
            items = json.loads(json.loads(request.data)["messages"][1]["content"])["candidates"]
            self.assertFalse(BodyAttempt.objects.exists())
            screened.extend(item["candidate_id"] for item in items)
            return response({"decisions": [{"candidate_id": item["candidate_id"], "selected": True,
                "priority": priorities[item["candidate_id"]], "reason": "有重要新事件，需核对正文。"} for item in items]})
        def capture(request, **kwargs):
            self.assertCountEqual(screened, [c.pk for c in candidates])
            read.append(json.loads(request.data)["url"])
            return scrape_response()
        model = Mock(return_value=response({"assessments": [{"assumption_key": "pillar:0", "direction": "unknown",
            "explanation": "缺少同口径验证。", "quote": ""}]}))
        with patch("investment_watch.screening._default_transport", side_effect=screen), patch("investment_watch.screening._chat_url", return_value="https://example.ai/v1/chat/completions"), patch("investment_watch.body_capture.capture_transport", side_effect=capture), patch("investment_watch.body_capture.validate_public_http_url", side_effect=lambda u: u), patch("investment_watch.analysis._default_transport", model), patch("investment_watch.analysis._chat_url", return_value="https://example.ai/v1/chat/completions"):
            count, message, errors = process_pipeline(self.dossier)
        self.assertEqual(count, 3)
        self.assertFalse(errors)
        self.assertEqual(read, [c.material_version.url for c in candidates[:3]])
        self.assertEqual(ScreeningBatch.objects.filter(status="completed").count(), 2)

    def test_unknown_rows_are_collapsed_on_dynamic_list(self):
        c = self.make_candidate(1)
        for i in range(7):
            ThesisEvidence.objects.create(candidate=c, revision=c.revision, assumption_key=f"pillar:{i}",
                direction="unknown", explanation="没有足够证据。", input_key="a" * 64)
        self.client.force_login(self.user)
        page = self.client.get(reverse("investment_watch:items"), {"scope": "all"})
        self.assertContains(page, 'class="iw-more-evidence"')
        self.assertContains(page, "7 项证据不足")
        self.assertNotContains(page, '<details class="iw-more-evidence" open')
