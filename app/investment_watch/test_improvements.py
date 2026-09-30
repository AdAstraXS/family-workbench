import json
import os
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django.core.exceptions import PermissionDenied
from investment_research.models import ResearchDossier
from portfolio.models import Security
from . import tests as fixtures
from .models import NewsSource, ResearchCandidate, WatchRule, MaterialRelation
from .services import ingest, associate, Conflict, WatchError
from .workspace import add_company, preview_rule
from .source_templates import SourceForm, parse_source
from .events import relate, canonical_version, canonical_candidate
from .worker import pending_candidates, run_cycle
from .research_bridge import append_news
from .analysis import analyze_candidate, set_consent


class ImprovementTests(TestCase):
    setUpTestData = classmethod(fixtures.WatchTests.setUpTestData.__func__)

    def test_more_than_eight_sources_are_not_starved(self):
        for i in range(10):
            NewsSource.objects.create(
                family=self.family,
                key=f"extra-{i}",
                name=f"Source {i}",
                url=f"https://example.com/{i}",
                enabled=True,
            )
        checked = []

        def collect(source):
            checked.append(source.pk)
            source.last_checked_at = timezone.now()
            source.save(update_fields=["last_checked_at"])
            return {"status": "success", "added": 0}

        with (
            patch("investment_watch.worker.collect_source", side_effect=collect),
            patch("investment_watch.worker.import_official", return_value=0),
        ):
            run_cycle(self.family, analyze=False)
            run_cycle(self.family, analyze=False)
        self.assertEqual(len(checked), 11)
        self.assertEqual(len(set(checked)), 11)

    @override_settings(INVESTMENT_WATCH_MODEL_ENABLED=True)
    def test_confirmed_reprint_does_not_make_second_paid_call(self):
        copy = self.duplicate()
        relate(
            self.other, copy.pk, self.version.pk, "duplicate", "同一公告，无新增内容", 0
        )
        original = associate(
            self.member,
            self.dossier.pk,
            self.version.pk,
            self.dossier.current_revision_id,
        )
        reprint = associate(
            self.member, self.dossier.pk, copy.pk, self.dossier.current_revision_id
        )
        set_consent(self.member, self.dossier.pk, self.provider, True)
        output = {
            "assessments": [
                {
                    "assumption_key": "pillar:0",
                    "direction": "unknown",
                    "explanation": "摘录不足",
                    "quote": "",
                    "conditions": "核查财报",
                    "gaps": "无现金数据",
                    "source_claim": "报道云需求",
                    "author_opinion": "无法区分",
                }
            ]
        }
        response = json.dumps(
            {
                "choices": [{"message": {"content": json.dumps(output)}}],
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
            self.assertEqual(analyze_candidate(reprint.pk), 1)
            self.assertEqual(analyze_candidate(original.pk), 0)
            self.assertEqual(analyze_candidate(reprint.pk), 0)
            self.assertEqual(transport.call_count, 1)

    def test_exact_content_automatically_shares_canonical_material(self):
        other_source = NewsSource.objects.create(
            family=self.family,
            key="reprints",
            name="Reprint",
            url="https://example.org",
        )
        copy, _ = ingest(
            other_source,
            external_id="one",
            title=self.version.title,
            summary=self.version.summary,
            published_at=self.version.published_at,
            url="https://example.org/one",
        )
        self.assertEqual(canonical_version(copy).pk, self.version.pk)

    def test_changed_original_requires_new_relation_review(self):
        copy = self.duplicate()
        relate(self.other, copy.pk, self.version.pk, "duplicate", "同一公告", 0)
        ingest(
            self.source,
            external_id="one",
            title="Corrected title",
            summary="Correction",
            url=self.version.url,
        )
        self.assertEqual(canonical_version(copy).pk, copy.pk)

    def test_company_topic_is_private_and_keeps_pause_readable(self):
        dossier = add_company(self.member, {"security": self.security})
        self.client.force_login(self.user)
        response = self.client.get(
            reverse("investment_watch:news"), {"dossier": dossier.pk}
        )
        self.assertContains(response, self.version.title)
        self.client.force_login(self.other_user)
        self.assertEqual(
            self.client.get(
                reverse("investment_watch:news"), {"dossier": dossier.pk}
            ).status_code,
            404,
        )

    def test_association_selector_retains_revision_check(self):
        self.client.force_login(self.user)
        data = {
            "selection": f"{self.dossier.pk}:{self.dossier.current_revision_id}",
            "material_version": self.version.pk,
            "idempotency_key": "selection-test",
        }
        response = self.client.post(
            reverse("investment_watch:associate", args=[self.version.material_id]), data
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            ResearchCandidate.objects.filter(dossier=self.dossier).count(), 1
        )

    def test_company_add_reuses_security_and_private_dossier(self):
        data = {"name": "Apple", "symbol": "aapl", "market": "US"}
        dossier = add_company(self.member, data)
        self.assertIsNone(dossier.current_revision_id)
        self.assertFalse(WatchRule.objects.get(dossier=dossier).enabled)
        self.assertEqual(add_company(self.member, data).pk, dossier.pk)
        other = add_company(self.other, data)
        self.assertNotEqual(other.pk, dossier.pk)
        self.assertEqual(other.security_id, dossier.security_id)
        self.assertEqual(Security.objects.filter(symbol="AAPL").count(), 1)

    def test_china_company_can_be_followed_without_thesis(self):
        dossier = add_company(
            self.member, {"name": "示例公司", "symbol": "600001", "market": "SH"}
        )
        self.assertEqual(dossier.security.currency, "CNY")
        self.client.force_login(self.user)
        self.assertEqual(
            self.client.get(
                reverse("investment_watch:company", args=[dossier.pk])
            ).status_code,
            200,
        )

    def test_company_and_source_pages_are_scoped(self):
        self.client.force_login(self.other_user)
        self.assertEqual(
            self.client.get(
                reverse("investment_watch:company", args=[self.dossier.pk])
            ).status_code,
            404,
        )
        self.client.force_login(self.user)
        self.assertEqual(
            self.client.get(reverse("investment_watch:source_add")).status_code, 403
        )

    def test_preview_is_read_only_and_uses_unsaved_words(self):
        before = ResearchCandidate.objects.count()
        result = preview_rule(
            self.member,
            {"aliases": ["Microsoft"], "exclude": [], "topics": [], "include": []},
        )
        self.assertEqual(result["matched"], 1)
        self.assertEqual(ResearchCandidate.objects.count(), before)
        self.assertFalse(WatchRule.objects.filter(dossier=self.dossier).exists())
        result = preview_rule(
            self.member,
            {
                "aliases": ["Microsoft"],
                "exclude": ["cloud"],
                "topics": [],
                "include": [],
            },
        )
        self.assertEqual(result["matched"], 0)

    def test_preview_post_does_not_save(self):
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("investment_watch:rules"),
            {
                "action": "preview",
                "dossier_id": self.dossier.pk,
                "expected_version": 0,
                "aliases": "Microsoft",
                "enabled": "on",
            },
        )
        self.assertContains(response, "命中 1 份")
        self.assertFalse(WatchRule.objects.filter(dossier=self.dossier).exists())

    def test_json_and_html_templates_keep_unknown_dates(self):
        source = SimpleNamespace(
            adapter="json",
            url="https://example.com/feed",
            max_items=10,
            config={
                "items": "data.items",
                "title": "title",
                "url": "link",
                "date": "date",
            },
        )
        rows = parse_source(
            json.dumps(
                {
                    "data": {
                        "items": [
                            {
                                "title": "Industry news",
                                "link": "/one",
                                "date": "unknown",
                            }
                        ]
                    }
                }
            ),
            source,
        )
        self.assertEqual(rows[0]["url"], "https://example.com/one")
        self.assertIsNone(rows[0]["published_at"])
        source.adapter = "html"
        source.config = {"items": "article", "title": "h2", "url": "a@href"}
        rows = parse_source(
            '<article><h2>Company news</h2><a href="/two">Read</a></article>', source
        )
        self.assertEqual(rows[0]["title"], "Company news")
        with self.assertRaises(WatchError):
            parse_source(
                '<article><h2>Unsafe</h2><a href="javascript:alert(1)">Read</a></article>',
                source,
            )

    def test_source_requires_successful_test_of_exact_config(self):
        self.client.force_login(self.other_user)
        url = reverse("investment_watch:source_add")
        data = {
            "name": "Industry blog",
            "url": "https://example.com/feed",
            "adapter": "json",
            "market": "全球",
            "interval_minutes": 120,
            "max_items": 20,
            "config": json.dumps({"items": "items", "title": "title", "url": "url"}),
        }
        before = NewsSource.objects.count()
        response = self.client.post(url, {**data, "action": "save"})
        self.assertContains(response, "请先测试读取")
        self.assertEqual(NewsSource.objects.count(), before)
        with patch(
            "intelligence.http_client.fetch_public_url",
            return_value=SimpleNamespace(
                body=b'{"items":[{"title":"News","url":"https://example.com/one"}]}'
            ),
        ):
            response = self.client.post(url, {**data, "action": "test"})
        token = response.context["tested"]
        self.assertTrue(token)
        self.assertEqual(NewsSource.objects.count(), before)
        self.client.post(
            url, {**data, "name": "Changed", "action": "save", "tested": token}
        )
        self.assertEqual(NewsSource.objects.count(), before)
        response = self.client.post(url, {**data, "action": "save", "tested": token})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(NewsSource.objects.get(name="Industry blog").enabled)

    def test_source_form_rejects_credentials_and_invalid_config(self):
        form = SourceForm(
            {
                "name": "bad",
                "url": "https://user:password@example.com/feed",
                "adapter": "rss",
                "market": "全球",
                "interval_minutes": 1,
                "max_items": 100,
            }
        )
        self.assertFalse(form.is_valid())

    def duplicate(self):
        return ingest(
            self.source,
            external_id="copy",
            title="Different media headline",
            summary="Reprinted disclosure",
            url="https://example.com/copy",
        )[0]

    def test_confirmed_duplicate_uses_one_candidate_and_one_packet_item(self):
        copy = self.duplicate()
        relate(
            self.other,
            copy.pk,
            self.version.pk,
            "duplicate",
            "核对原公告，无新增信息",
            0,
        )
        candidate = associate(
            self.member, self.dossier.pk, copy.pk, self.dossier.current_revision_id
        )
        original = canonical_candidate(candidate)
        self.assertEqual(original.material_version_id, self.version.pk)
        pending = pending_candidates(self.dossier, self.provider)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].pk, original.pk)
        ResearchCandidate.objects.filter(dossier=self.dossier).update(
            selected_for_research=True
        )
        self.assertEqual(
            len(
                append_news({"evidence": [], "sources": []}, self.dossier)[
                    "news_snapshots"
                ]
            ),
            1,
        )

    def test_relation_history_permission_conflict_and_version_invalidation(self):
        copy = self.duplicate()
        with self.assertRaises(PermissionDenied):
            relate(self.member, copy.pk, self.version.pk, "duplicate", "重复", 0)
        relation = relate(self.other, copy.pk, self.version.pk, "duplicate", "重复", 0)
        with self.assertRaises(Conflict):
            relate(self.other, copy.pk, 0, "independent", "撤销", 0)
        self.assertEqual(canonical_version(copy).pk, self.version.pk)
        relate(self.other, copy.pk, 0, "independent", "发现新的采访内容", relation.pk)
        self.assertEqual(canonical_version(copy).pk, copy.pk)
        self.assertEqual(MaterialRelation.objects.count(), 2)

    def test_followup_does_not_suppress_analysis(self):
        copy = self.duplicate()
        relate(self.other, copy.pk, self.version.pk, "followup", "新增采访", 0)
        self.assertEqual(canonical_version(copy).pk, copy.pk)

    def test_original_can_arrive_later_but_cycles_are_rejected(self):
        original = self.duplicate()
        relate(self.other, self.version.pk, original.pk, "duplicate", "原始公告晚于转载被采集", 0)
        self.assertEqual(canonical_version(self.version).pk, original.pk)
        with self.assertRaises(WatchError):
            relate(self.other, original.pk, self.version.pk, "duplicate", "错误循环", 0)

    def test_new_pages_render_without_creating_dossiers(self):
        self.client.force_login(self.user)
        before = ResearchDossier.objects.count()
        for name, args in [
            ("company_add", []),
            ("company", [self.dossier.pk]),
            ("news_detail", [self.version.material_id]),
            ("rules", []),
        ]:
            self.assertEqual(
                self.client.get(
                    reverse("investment_watch:" + name, args=args)
                ).status_code,
                200,
            )
        self.assertEqual(ResearchDossier.objects.count(), before)
