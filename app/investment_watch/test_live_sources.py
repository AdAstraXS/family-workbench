"""Regressions from real public-list acceptance; no network or paid calls."""

from datetime import timedelta
from decimal import Decimal
import json
import os
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from intelligence.http_client import FetchResponse
from . import tests as fixtures
from .collection import collect_source, parse_list
from .models import MaterialVersion, ResearchCandidate
from .models import BudgetReceipt, ThesisEvidence
from .services import associate, candidate_stale, current_evidence, ingest, review
from .source_templates import parse_source
from .analysis import analyze_candidate, set_consent, validate_result
from .evaluation import evaluate_recall


class LiveSourceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        fixtures.WatchTests.setUpTestData.__func__(cls)

    def test_list_card_wins_over_duplicate_carousel_without_guessing_relative_date(self):
        source = SimpleNamespace(
            adapter="zhitong", url="https://www.zhitongcaijing.com/", max_items=40
        )
        html = '''<div class="banner-swipe-item-box">
          <a href="/content/detail/123.html">云行业相关新闻</a></div>
          <div class="info-list-item"><div class="info-item-content"><div>
          <div class="info-item-content-title">
          <a href="/content/detail/123.html">云行业相关新闻</a></div>
          <div class="info-item-content-desc">公开列表的完整摘要，保留反面信息。</div>
          </div><div class="info-item-content-operat"><span>56分钟前</span>
          </div></div></div>'''
        rows = parse_list(html, source)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["summary"], "公开列表的完整摘要，保留反面信息。")
        self.assertIsNone(rows[0]["published_at"])
        self.assertEqual(rows[0]["published_precision"], "unknown")

    def test_invalid_calendar_date_does_not_abort_the_public_list(self):
        source = SimpleNamespace(
            adapter="caixin", url="https://finance.caixin.com/", max_items=40
        )
        rows = parse_list('''<div><h3>
          <a href="https://finance.caixin.com/2026-02-31/123.html">公开金融新闻</a>
          </h3><p>目录摘要</p></div>''', source)
        self.assertIsNone(rows[0]["published_at"])
        self.assertEqual(rows[0]["published_precision"], "unknown")

    def test_comment_links_and_duplicate_hover_titles_are_not_news(self):
        source = SimpleNamespace(
            adapter="caixin", url="https://finance.caixin.com/", max_items=40
        )
        rows = parse_list('''<div><h3>
          <a href="https://finance.caixin.com/2026-09-30/123.html">公开金融报道</a>
          </h3><p>目录摘要</p><a href="https://finance.caixin.com/2026-09-30/123.html#gocomment">
          评论(<em>0</em>)</a></div>''', source)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "公开金融报道")
        source.adapter = "zhitong"
        source.url = "https://www.zhitongcaijing.com/"
        rows = parse_list('''<div><a href="/content/detail/123.html">
          <div class="show-text">行业相关新闻</div>
          <div class="show-text-hover">行业相关新闻</div></a></div>''', source)
        self.assertEqual(rows[0]["title"], "行业相关新闻")

    def test_template_date_only_and_explicit_status_are_preserved(self):
        source = SimpleNamespace(
            adapter="html", url="https://example.com/", max_items=10,
            config={"items": "article", "title": "h2", "url": "a@href",
                    "date": "time@datetime", "status": "@data-status"},
        )
        rows = parse_source('''<article data-status="corrected"><h2>公开更正通知</h2>
          <a href="/one">原文</a><time datetime="2026-09-30"></time></article>''', source)
        self.assertEqual(rows[0]["status"], "corrected")
        self.assertEqual(rows[0]["published_precision"], "day")
        self.assertEqual(timezone.localdate(rows[0]["published_at"]).isoformat(), "2026-09-30")

    @override_settings(INVESTMENT_WATCH_COLLECT_ENABLED=True)
    def test_bad_row_is_reported_without_losing_valid_results_or_advancing_cursor(self):
        body = b'''<rss version="2.0"><channel>
          <item><guid>good</guid><title>Company financial update</title>
          <link>https://example.com/good</link></item>
          <item><guid>bad</guid><title>Another company update</title>
          <link>http://127.0.0.1/private</link></item></channel></rss>'''
        response = FetchResponse(200, self.source.url, body, etag="new")
        result = collect_source(self.source, fetcher=lambda *a, **k: response, force=True)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["added"], 1)
        self.assertEqual(result["rejected"], 1)
        self.source.refresh_from_db()
        self.assertEqual(self.source.cursor, {})
        self.assertIsNone(self.source.last_success_at)
        self.assertTrue(self.source.last_error)

    @override_settings(INVESTMENT_WATCH_COLLECT_ENABLED=True)
    def test_explicit_atom_tombstone_preserves_history_and_invalidates_candidate(self):
        candidate = associate(self.member, self.dossier.pk, self.version.pk,
                              self.dossier.current_revision_id)
        body = b'''<feed xmlns="http://www.w3.org/2005/Atom"
          xmlns:at="http://purl.org/atompub/tombstones/1.0">
          <title>Public feed</title><at:deleted-entry ref="one" when="2026-10-02T00:00:00Z"/>
          <at:deleted-entry ref="not-collected" when="2026-10-02T00:00:00Z"/></feed>'''
        response = FetchResponse(200, self.source.url, body)
        result = collect_source(self.source, fetcher=lambda *a, **k: response, force=True)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["added"], 1)
        self.version.material.refresh_from_db()
        latest = self.version.material.current_version
        self.assertEqual(latest.status, "withdrawn")
        self.assertEqual(latest.published_at, self.version.published_at)
        self.assertEqual(latest.number, 2)
        candidate = ResearchCandidate.objects.select_related(
            "dossier", "material_version__material"
        ).get(pk=candidate.pk)
        self.assertTrue(candidate_stale(candidate))
        self.assertEqual(MaterialVersion.objects.get(pk=self.version.pk).status, "active")
        again = collect_source(self.source, fetcher=lambda *a, **k: response, force=True)
        self.assertEqual(again["added"], 0)

    @override_settings(INVESTMENT_WATCH_COLLECT_ENABLED=True)
    def test_list_absence_does_not_withdraw_old_items(self):
        response = FetchResponse(200, self.source.url, b'<rss><channel/></rss>')
        collect_source(self.source, fetcher=lambda *a, **k: response, force=True)
        self.version.material.refresh_from_db()
        self.assertEqual(self.version.material.current_version_id, self.version.pk)

    @override_settings(INVESTMENT_WATCH_COLLECT_ENABLED=True)
    def test_known_old_material_can_receive_a_correction_outside_intake_window(self):
        old_date = timezone.now() - timedelta(days=120)
        old, _ = ingest(self.source, external_id="old", title="Historical company results",
                        summary="Initial result", url="https://example.com/old",
                        published_at=old_date)
        row = {"external_id": "old", "title": old.title, "summary": "Corrected result",
               "url": old.url, "published_at": old_date, "status": "corrected"}
        with patch("investment_watch.source_templates.parse_source", return_value=[row]):
            result = collect_source(self.source,
                fetcher=lambda *a, **k: FetchResponse(200, self.source.url, b"<rss><channel/></rss>"),
                force=True)
        self.assertEqual(result["added"], 1)
        old.material.refresh_from_db()
        self.assertEqual(old.material.current_version.status, "corrected")

    def test_outer_json_fence_is_accepted_but_commentary_and_invented_quotes_are_rejected(self):
        candidate = associate(self.member, self.dossier.pk, self.version.pk,
                              self.dossier.current_revision_id)
        result = {"assessments": [{"assumption_key": "pillar:0", "direction": "unknown",
                                  "explanation": "材料不足，等待核查。"}]}
        raw = json.dumps(result, ensure_ascii=False)
        self.assertEqual(len(validate_result("```json\n" + raw + "\n```", candidate)), 1)
        with self.assertRaisesMessage(fixtures.WatchError, "JSON"):
            validate_result(raw + "\nAdditional claims", candidate)
        result["assessments"][0].update(direction="support", quote="Invented quotation")
        with self.assertRaisesMessage(fixtures.WatchError, "引文"):
            validate_result("```json\n" + json.dumps(result) + "\n```", candidate)

    def test_null_quote_is_only_accepted_for_unknown_and_direction_requires_conditions(self):
        candidate = associate(self.member, self.dossier.pk, self.version.pk,
                              self.dossier.current_revision_id)
        row = {"assumption_key": "pillar:0", "direction": "unknown",
               "explanation": "缺少实际收入数据。", "quote": None}
        self.assertEqual(validate_result(json.dumps({"assessments": [row]}), candidate)[0]["quote"], "")
        row["direction"] = "support"
        with self.assertRaisesMessage(fixtures.WatchError, "引文"):
            validate_result(json.dumps({"assessments": [row]}), candidate)
        row["quote"] = "Cloud demand is growing"
        with self.assertRaisesMessage(fixtures.WatchError, "成立条件"):
            validate_result(json.dumps({"assessments": [row]}), candidate)

    def test_recall_labels_are_frozen_unique_and_family_scoped(self):
        rule = SimpleNamespace(dossier=self.dossier, version=1, enabled=True,
                               aliases=["Cloud"], include=[], exclude=[], topics=[])
        case = {"version_id": self.version.pk, "expected_recall": True,
                "content_hash": self.version.content_hash}
        versions = {self.version.pk: self.version}
        self.assertEqual(evaluate_recall(rule, [case], versions)["counts"]["true_positive"], 1)
        for cases in ([case, case], [{**case, "expected_recall": 1}],
                      [{**case, "content_hash": "changed"}]):
            with self.assertRaises(fixtures.WatchError):
                evaluate_recall(rule, cases, versions)
        rule.dossier = SimpleNamespace(family_id=self.dossier.family_id + 1)
        with self.assertRaises(fixtures.WatchError):
            evaluate_recall(rule, [case], versions)

    def test_reanalysis_uses_latest_batch_while_preserving_uneditable_history(self):
        candidate = associate(self.member, self.dossier.pk, self.version.pk,
                              self.dossier.current_revision_id)
        old = ThesisEvidence.objects.create(candidate=candidate, revision=candidate.revision,
            input_key="old", assumption_key="pillar:0", direction="unknown",
            explanation="旧分析")
        latest = ThesisEvidence.objects.create(candidate=candidate, revision=candidate.revision,
            input_key="new", assumption_key="pillar:0", direction="unknown",
            explanation="新分析")
        self.assertEqual([e.pk for e in current_evidence(candidate)], [latest.pk])
        with self.assertRaises(fixtures.Conflict):
            review(self.member, old.pk, "unknown", "尝试修改历史")
        self.client.force_login(self.user)
        listing = self.client.get("/research/watch/items/?format=json").json()
        self.assertEqual([e["id"] for e in listing["items"][0]["evidence"]], [latest.pk])
        detail = self.client.get(f"/research/watch/items/{candidate.pk}/?format=json").json()
        self.assertEqual({e["id"]: e["is_current"] for e in detail["evidence"]},
                         {old.pk: False, latest.pk: True})
        self.assertTrue(ThesisEvidence.objects.filter(pk=old.pk).exists())

    @override_settings(INVESTMENT_WATCH_MODEL_ENABLED=True)
    def test_failed_output_still_records_verified_returned_usage(self):
        candidate = associate(self.member, self.dossier.pk, self.version.pk,
                              self.dossier.current_revision_id)
        set_consent(self.member, self.dossier.pk, self.provider, True)
        response = json.dumps({"choices": [{"finish_reason": "stop",
                                "message": {"content": "Invalid JSON"}}],
                               "usage": {"prompt_tokens": 100, "completion_tokens": 50}})
        with patch.dict(os.environ, WATCH_TEST_KEY="local-test"):
            with self.assertRaisesMessage(fixtures.WatchError, "已记录返回用量"):
                analyze_candidate(candidate.pk, transport=lambda *a, **k: response,
                                  url_validator=lambda p: "https://example.ai/chat/completions")
        receipt = BudgetReceipt.objects.get()
        self.assertEqual(receipt.status, "failed")
        self.assertEqual(receipt.actual_cny, Decimal("0.0014"))
        self.assertFalse(ThesisEvidence.objects.exists())

    @override_settings(INVESTMENT_WATCH_MODEL_ENABLED=True)
    def test_glm53_request_bounds_reasoning_and_requests_json(self):
        self.provider.base_url = "https://open.bigmodel.cn/api/paas/v4"
        self.provider.model_name = "glm-5.3-flashx"
        self.provider.extra_data["research_max_output_tokens"] = 4000
        self.provider.save()
        candidate = associate(self.member, self.dossier.pk, self.version.pk,
                              self.dossier.current_revision_id)
        set_consent(self.member, self.dossier.pk, self.provider, True)
        sent = []
        result = {"assessments": [{"assumption_key": "pillar:0", "direction": "unknown",
                                  "explanation": "仅为测试材料，不能证明增长。"}]}
        def transport(request, **kwargs):
            sent.append(json.loads(request.data))
            return json.dumps({"choices": [{"finish_reason": "stop",
                "message": {"content": json.dumps(result)}}]})
        with patch.dict(os.environ, WATCH_TEST_KEY="local-test"):
            analyze_candidate(candidate.pk, transport=transport,
                              url_validator=lambda p: "https://example.ai/chat/completions")
        self.assertEqual(sent[0]["max_tokens"], 4000)
        self.assertEqual(sent[0]["reasoning_effort"], "low")
        self.assertEqual(sent[0]["thinking"], {"type": "enabled"})
        self.assertEqual(sent[0]["response_format"], {"type": "json_object"})
