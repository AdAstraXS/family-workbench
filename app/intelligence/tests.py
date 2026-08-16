from datetime import timedelta
from decimal import Decimal
from io import StringIO
import json
import tempfile
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management.base import CommandError
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from family_core.models import Family, FamilyMember
from ai_analysis.models import AiAnalysisRequest, AiAnalysisResult, AiProvider
from knowledge.models import (
    KnowledgeDocument,
    KnowledgeRevision,
    KnowledgeSearchEntry,
    KnowledgeSource,
)

from .forms import IntelligenceSourceForm, IntelligenceSubjectForm
from .adapters import AdapterResult, CollectedItem, FeedParseError, parse_rss_or_atom, parse_youtube_atom
from .collection import collect_intelligence_sources
from .ai_enrichment import (
    IntelligenceAiError,
    analyze_event,
    parse_analysis_result,
    provider_is_configured,
)
from .http_client import FetchResponse, SafeHttpError, validate_public_http_url
from .management.commands.seed_intelligence_sources import SOURCE_DEFINITIONS
from .management.commands.seed_key_people import SUBJECTS
from .models import (
    CollectionRun,
    EventAnalysis,
    EventEvidence,
    EventKnowledgeArchive,
    EventSubject,
    EventUserState,
    IntelligenceEvent,
    IntelligenceSource,
    IntelligenceSubject,
    SourceItem,
    SubjectFollow,
    SubjectKnowledgeIdentity,
)
from .processing import MEDIA_DISCOVERY_POLICY, process_source_item
from .scoring import calculate_event_scores


class IntelligenceTestBase(TestCase):
    def setUp(self):
        User = get_user_model()
        self.family = Family.objects.create(name="情报测试家庭")
        self.admin_user = User.objects.create_user(username="intel-admin", password="test-password")
        self.admin_member = FamilyMember.objects.create(
            family=self.family,
            user=self.admin_user,
            display_name="情报管理员",
            role=FamilyMember.ROLE_ADMIN,
        )
        self.member_user = User.objects.create_user(username="intel-member", password="test-password")
        self.member = FamilyMember.objects.create(
            family=self.family,
            user=self.member_user,
            display_name="家庭成员",
            role=FamilyMember.ROLE_MEMBER,
        )
        self.viewer_user = User.objects.create_user(username="intel-viewer", password="test-password")
        self.viewer = FamilyMember.objects.create(
            family=self.family,
            user=self.viewer_user,
            display_name="只读成员",
            role=FamilyMember.ROLE_VIEWER,
        )
        self.subject = IntelligenceSubject.objects.create(
            subject_type=IntelligenceSubject.TYPE_PERSON,
            canonical_name="Test Person",
            display_name="测试人物",
            category=IntelligenceSubject.CATEGORY_TECH_LEADER,
            aliases=["测试人物"],
        )
        self.source = IntelligenceSource.objects.create(
            subject=self.subject,
            source_type=IntelligenceSource.TYPE_MANUAL,
            adapter_key="manual",
            name="测试原始来源",
            source_tier=IntelligenceSource.TIER_A,
            source_group=IntelligenceSource.GROUP_OFFICIAL,
        )
        self.source.topics.add(self.subject)

    def make_event(
        self,
        *,
        family=None,
        status=IntelligenceEvent.REVIEW_PUBLISHED,
        selection=IntelligenceEvent.SELECTION_SELECTED,
        title="测试动态",
    ):
        family = family or self.family
        item = SourceItem.objects.create(
            source=self.source,
            title=f"{title}原文",
            canonical_url="https://example.com/source",
            processing_status=SourceItem.STATUS_CLUSTERED,
        )
        event = IntelligenceEvent.objects.create(
            family=family,
            event_type=IntelligenceEvent.TYPE_STATEMENT,
            title=title,
            occurred_at=timezone.now() - timedelta(hours=1),
            summary="这是一个可核查的事实摘要。",
            why_it_matters="用于验证情报信息流。",
            importance_score=80,
            confidence_score=90,
            review_status=status,
            selection_status=selection,
            primary_source_item=item,
            created_by=self.admin_user,
            updated_by=self.admin_user,
        )
        EventSubject.objects.create(
            event=event,
            subject=self.subject,
            role=EventSubject.ROLE_SUBJECT,
            is_primary=True,
            confidence_score=100,
        )
        EventEvidence.objects.create(
            event=event,
            source_item=item,
            evidence_type=EventEvidence.TYPE_FACT,
            source_quality_score=95,
            is_primary=True,
        )
        return event

    def manual_event_payload(self):
        occurred_at = timezone.localtime().replace(second=0, microsecond=0)
        return {
            "subject": self.subject.pk,
            "event_type": IntelligenceEvent.TYPE_INTERVIEW,
            "title": "测试人物发布重要访谈",
            "occurred_at": occurred_at.strftime("%Y-%m-%dT%H:%M"),
            "occurred_precision": IntelligenceEvent.PRECISION_EXACT,
            "summary": "测试人物在公开访谈中确认了新的业务方向。",
            "why_it_matters": "这可能改变相关行业的资本开支预期。",
            "relevance_score": 85,
            "impact_score": 85,
            "novelty_score": 70,
            "actionability_score": 70,
            "timeliness_score": 85,
            "change_type": IntelligenceEvent.CHANGE_NEW,
            "review_status": IntelligenceEvent.REVIEW_PUBLISHED,
            "source_name": "测试访谈",
            "source_tier": IntelligenceSource.TIER_B,
            "source_group": IntelligenceSource.GROUP_EXPERT,
            "source_title": "测试人物完整访谈",
            "source_url": "https://example.com/interview/1",
            "source_author": "测试媒体",
            "evidence_type": EventEvidence.TYPE_FACT,
            "evidence_excerpt": "确认新的业务方向。",
        }


class IntelligenceModelsAndCommandsTests(IntelligenceTestBase):
    def test_subject_slug_is_created_once_and_remains_stable(self):
        original_slug = self.subject.slug
        self.subject.display_name = "修改后的显示名"
        self.subject.save()
        self.subject.refresh_from_db()

        self.assertEqual(original_slug, "test-person")
        self.assertEqual(self.subject.slug, original_slug)

    def test_source_form_allows_registering_but_not_running_future_adapter(self):
        form = IntelligenceSourceForm(
            data={
                "subject": self.subject.pk,
                "topics": [self.subject.pk],
                "source_type": IntelligenceSource.TYPE_RSS,
                "source_group": IntelligenceSource.GROUP_OFFICIAL,
                "adapter_key": "rss",
                "name": "尚未启用的 RSS",
                "url": "https://example.com/feed.xml",
                "external_id": "",
                "source_tier": IntelligenceSource.TIER_A,
                "transport_weight": 100,
                "poll_interval_minutes": 60,
                "is_active": True,
            }
        )

        self.assertTrue(form.is_valid(), form.errors)
        source = form.save()
        self.assertEqual(source.adapter_key, IntelligenceSource.ADAPTER_RSS)
        self.assertTrue(source.is_due)

    def test_source_form_requires_at_least_one_topic(self):
        form = IntelligenceSourceForm(
            data={
                "subject": "",
                "topics": [],
                "source_type": IntelligenceSource.TYPE_RSS,
                "source_group": IntelligenceSource.GROUP_OFFICIAL,
                "adapter_key": IntelligenceSource.ADAPTER_RSS,
                "name": "未关联主题的 RSS",
                "url": "https://example.com/unmapped.xml",
                "external_id": "",
                "source_tier": IntelligenceSource.TIER_A,
                "transport_weight": 100,
                "poll_interval_minutes": 60,
                "is_active": True,
            }
        )

        self.assertFalse(form.is_valid())
        self.assertIn("topics", form.errors)

    def test_subject_form_accepts_plain_text_aliases(self):
        form = IntelligenceSubjectForm(
            data={
                "subject_type": IntelligenceSubject.TYPE_PERSON,
                "canonical_name": "Plain Alias Person",
                "display_name": "普通别名人物",
                "category": IntelligenceSubject.CATEGORY_INVESTOR,
                "aliases": "Plain Person\n普通人物，Plain Person",
                "profile_summary": "",
                "avatar_url": "",
                "importance_level": 3,
                "is_active": True,
            }
        )

        self.assertTrue(form.is_valid(), form.errors)
        subject = form.save()
        self.assertEqual(subject.aliases, ["Plain Person", "普通人物"])

    def test_subject_form_saves_explicit_knowledge_author_identities(self):
        form = IntelligenceSubjectForm(
            data={
                "subject_type": self.subject.subject_type,
                "canonical_name": self.subject.canonical_name,
                "display_name": self.subject.display_name,
                "category": self.subject.category,
                "aliases": "测试人物",
                "knowledge_author_names": "历史署名\n旧署名，历史署名",
                "profile_summary": "",
                "avatar_url": "",
                "importance_level": 3,
                "is_active": True,
            },
            instance=self.subject,
            family=self.family,
        )

        self.assertTrue(form.is_valid(), form.errors)
        subject = form.save()
        form.save_knowledge_identities(subject=subject, user=self.admin_user)

        self.assertEqual(
            list(
                SubjectKnowledgeIdentity.objects.filter(
                    family=self.family,
                    subject=self.subject,
                    is_active=True,
                ).values_list("author_name", flat=True)
            ),
            ["历史署名", "旧署名"],
        )

    def test_source_can_map_to_multiple_topics(self):
        technology = IntelligenceSubject.objects.create(
            subject_type=IntelligenceSubject.TYPE_TECHNOLOGY,
            canonical_name="AI Infrastructure Test",
            display_name="AI 基础设施测试",
            category=IntelligenceSubject.CATEGORY_TECHNOLOGY,
        )
        self.source.topics.add(technology)

        self.assertEqual(self.source.topics.count(), 2)
        self.assertIn(self.source, technology.sources.all())

    def test_scoring_policy_separates_importance_and_confidence(self):
        result = calculate_event_scores(
            relevance=10,
            impact=10,
            novelty=10,
            actionability=10,
            timeliness=30,
            source_tier=IntelligenceSource.TIER_A,
            has_url=True,
            has_excerpt=True,
            extraction_confidence=100,
        )

        self.assertLess(result.importance_score, 25)
        self.assertGreaterEqual(result.confidence_score, 75)
        self.assertEqual(result.selection_status, IntelligenceEvent.SELECTION_NOISE)

    def test_scoring_policy_selects_high_value_event(self):
        result = calculate_event_scores(
            relevance=100,
            impact=85,
            novelty=85,
            actionability=85,
            timeliness=85,
            source_tier=IntelligenceSource.TIER_B,
            has_url=True,
            has_excerpt=True,
            extraction_confidence=100,
        )

        self.assertGreaterEqual(result.importance_score, 75)
        self.assertGreaterEqual(result.confidence_score, 60)
        self.assertEqual(result.selection_status, IntelligenceEvent.SELECTION_SELECTED)

    def test_seed_command_dry_run_rolls_back(self):
        call_command("seed_key_people", "--dry-run", stdout=StringIO())

        self.assertEqual(IntelligenceSubject.objects.count(), 1)

    def test_seed_command_is_idempotent_and_can_follow_for_one_family(self):
        output = StringIO()
        call_command("seed_key_people", "--follow-all", stdout=output)
        call_command("seed_key_people", "--follow-all", stdout=output)

        self.assertEqual(IntelligenceSubject.objects.count(), len(SUBJECTS) + 1)
        self.assertEqual(
            SubjectFollow.objects.filter(family=self.family).count(),
            len(SUBJECTS),
        )


class IntelligenceViewsTests(IntelligenceTestBase):
    def test_admin_manual_create_builds_traceable_event_chain(self):
        self.client.force_login(self.admin_user)

        response = self.client.post(reverse("intelligence:event_create"), self.manual_event_payload())

        event = IntelligenceEvent.objects.get(title="测试人物发布重要访谈")
        self.assertRedirects(response, reverse("intelligence:event_detail", kwargs={"pk": event.pk}))
        self.assertEqual(event.family, self.family)
        self.assertEqual(event.created_by, self.admin_user)
        self.assertEqual(event.primary_source_item.created_by, self.admin_user)
        self.assertTrue(event.evidence_links.filter(source_item=event.primary_source_item, is_primary=True).exists())
        self.assertTrue(event.subject_links.filter(subject=self.subject, is_primary=True).exists())
        self.assertTrue(event.scoring_breakdown)
        self.assertEqual(event.scoring_policy_version, "people-v1")
        self.assertEqual(event.selection_status, IntelligenceEvent.SELECTION_SELECTED)
        self.assertIn(self.subject, event.primary_source_item.source.topics.all())
        self.assertEqual(CollectionRun.objects.filter(family=self.family, run_kind=CollectionRun.KIND_MANUAL).count(), 1)

    def test_duplicate_manual_submission_reuses_event_and_source_item(self):
        self.client.force_login(self.admin_user)
        payload = self.manual_event_payload()

        self.client.post(reverse("intelligence:event_create"), payload)
        self.client.post(reverse("intelligence:event_create"), payload)

        self.assertEqual(IntelligenceEvent.objects.filter(title=payload["title"]).count(), 1)
        self.assertEqual(SourceItem.objects.filter(title=payload["source_title"]).count(), 1)
        self.assertEqual(CollectionRun.objects.filter(run_kind=CollectionRun.KIND_MANUAL).count(), 2)
        self.assertEqual(CollectionRun.objects.filter(updated_count=1).count(), 1)

    def test_member_cannot_admin_but_can_follow_bookmark_and_read(self):
        event = self.make_event()
        self.client.force_login(self.member_user)

        self.assertEqual(self.client.get(reverse("intelligence:event_create")).status_code, 403)
        self.assertEqual(self.client.get(reverse("intelligence:subject_create")).status_code, 403)
        follow_response = self.client.post(reverse("intelligence:subject_toggle_follow", kwargs={"slug": self.subject.slug}))
        bookmark_response = self.client.post(reverse("intelligence:event_toggle_bookmark", kwargs={"pk": event.pk}))
        read_response = self.client.post(reverse("intelligence:event_mark_read", kwargs={"pk": event.pk}))

        self.assertEqual(follow_response.status_code, 302)
        self.assertEqual(bookmark_response.status_code, 302)
        self.assertEqual(read_response.status_code, 302)
        state = EventUserState.objects.get(member=self.member, event=event)
        self.assertIsNotNone(state.bookmarked_at)
        self.assertIsNotNone(state.read_at)
        self.assertTrue(SubjectFollow.objects.get(family=self.family, subject=self.subject).is_active)

    def test_viewer_can_read_but_middleware_blocks_all_posts(self):
        event = self.make_event()
        SubjectFollow.objects.create(family=self.family, subject=self.subject)
        self.client.force_login(self.viewer_user)

        self.assertEqual(self.client.get(reverse("intelligence:index")).status_code, 200)
        response = self.client.post(reverse("intelligence:event_mark_read", kwargs={"pk": event.pk}))

        self.assertEqual(response.status_code, 403)
        self.assertFalse(EventUserState.objects.filter(member=self.viewer, event=event).exists())

    def test_get_pages_do_not_create_user_state(self):
        event = self.make_event()
        SubjectFollow.objects.create(family=self.family, subject=self.subject)
        self.client.force_login(self.member_user)

        self.assertEqual(self.client.get(reverse("intelligence:index")).status_code, 200)
        self.assertEqual(self.client.get(reverse("intelligence:event_detail", kwargs={"pk": event.pk})).status_code, 200)
        self.assertFalse(EventUserState.objects.exists())

    def test_other_family_event_is_not_accessible(self):
        other_family = Family.objects.create(name="其他家庭")
        other_event = self.make_event(family=other_family, title="其他家庭私有动态")
        self.client.force_login(self.admin_user)

        response = self.client.get(reverse("intelligence:event_detail", kwargs={"pk": other_event.pk}))

        self.assertEqual(response.status_code, 404)

    def test_ignored_event_is_hidden_from_normal_stream(self):
        visible = self.make_event(title="正式动态")
        ignored = self.make_event(
            status=IntelligenceEvent.REVIEW_IGNORED,
            selection=IntelligenceEvent.SELECTION_NOISE,
            title="已忽略动态",
        )
        self.client.force_login(self.member_user)

        response = self.client.get(reverse("intelligence:event_list"))

        self.assertContains(response, visible.title)
        self.assertNotContains(response, ignored.title)

    def test_source_map_is_readable_but_pipeline_is_admin_only(self):
        self.client.force_login(self.member_user)

        source_response = self.client.get(reverse("intelligence:source_list"))
        pipeline_response = self.client.get(reverse("intelligence:pipeline"))

        self.assertEqual(source_response.status_code, 200)
        self.assertContains(source_response, self.source.name)
        self.assertContains(source_response, "RSS 与 YouTube 元数据采集已可用")
        self.assertEqual(pipeline_response.status_code, 403)

        self.client.force_login(self.admin_user)
        admin_pipeline_response = self.client.get(reverse("intelligence:pipeline"))
        self.assertEqual(admin_pipeline_response.status_code, 200)
        self.assertContains(admin_pipeline_response, "people-v1")

    def test_noise_event_is_only_visible_to_admin_noise_filter(self):
        noise = self.make_event(
            selection=IntelligenceEvent.SELECTION_NOISE,
            title="与投资科技无关的日常内容",
        )
        self.client.force_login(self.member_user)
        self.assertNotContains(self.client.get(reverse("intelligence:event_list")), noise.title)

        self.client.force_login(self.admin_user)
        response = self.client.get(
            reverse("intelligence:event_list"),
            {"selection": IntelligenceEvent.SELECTION_NOISE},
        )
        self.assertContains(response, noise.title)

    def test_pending_review_event_hides_actions_that_require_a_public_event(self):
        event = self.make_event(
            status=IntelligenceEvent.REVIEW_PENDING,
            selection=IntelligenceEvent.SELECTION_REVIEW,
            title="尚未复核的自动采集事件",
        )
        self.client.force_login(self.admin_user)

        detail = self.client.get(
            reverse("intelligence:event_detail", kwargs={"pk": event.pk})
        )

        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, "编辑事件")
        self.assertNotContains(detail, "标记已读")
        self.assertNotContains(detail, "收藏")
        self.assertNotContains(detail, "保存为知识")
        self.assertEqual(
            self.client.post(
                reverse("intelligence:event_mark_read", kwargs={"pk": event.pk})
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(
                reverse("intelligence:event_archive", kwargs={"pk": event.pk}),
                {"mode": EventKnowledgeArchive.MODE_ARCHIVE},
            ).status_code,
            404,
        )
        self.assertFalse(EventUserState.objects.filter(event=event).exists())
        self.assertFalse(EventKnowledgeArchive.objects.filter(event=event).exists())

    def test_next_url_redirects_do_not_append_detail_arguments(self):
        event = self.make_event()
        self.client.force_login(self.member_user)
        next_url = reverse("intelligence:event_list")

        response = self.client.post(
            reverse("intelligence:event_mark_read", kwargs={"pk": event.pk}),
            {"next": next_url},
        )

        self.assertRedirects(response, next_url)

    def test_external_next_url_is_rejected(self):
        event = self.make_event()
        self.client.force_login(self.member_user)

        response = self.client.post(
            reverse("intelligence:event_mark_read", kwargs={"pk": event.pk}),
            {"next": "https://malicious.example/redirect"},
        )

        self.assertRedirects(response, reverse("intelligence:event_detail", kwargs={"pk": event.pk}))


class IntelligenceKnowledgeBridgeTests(IntelligenceTestBase):
    def setUp(self):
        super().setUp()
        self.temp_directory = tempfile.TemporaryDirectory()
        self.storage = KnowledgeRevision._meta.get_field("raw_file").storage
        self.original_location = self.storage._location
        self.storage._location = self.temp_directory.name
        self.storage.__dict__.pop("base_location", None)
        self.storage.__dict__.pop("location", None)

    def tearDown(self):
        self.storage._location = self.original_location
        self.storage.__dict__.pop("base_location", None)
        self.storage.__dict__.pop("location", None)
        self.temp_directory.cleanup()
        super().tearDown()

    def test_member_archives_event_as_traceable_knowledge_snapshot(self):
        event = self.make_event(title="值得长期保存的动态")
        self.client.force_login(self.member_user)

        response = self.client.post(
            reverse("intelligence:event_archive", kwargs={"pk": event.pk}),
            {"mode": EventKnowledgeArchive.MODE_ARCHIVE},
        )

        self.assertRedirects(
            response,
            reverse("intelligence:event_detail", kwargs={"pk": event.pk}),
        )
        link = EventKnowledgeArchive.objects.select_related(
            "document__current_revision", "document__source"
        ).get(event=event)
        document = link.document
        self.assertEqual(link.archived_by, self.member)
        self.assertEqual(link.archive_mode, EventKnowledgeArchive.MODE_ARCHIVE)
        self.assertEqual(document.owner, self.member)
        self.assertEqual(document.source.kind, KnowledgeSource.KIND_INTELLIGENCE)
        self.assertEqual(document.knowledge_status, KnowledgeDocument.KNOWLEDGE_INCLUDED)
        self.assertEqual(document.current_revision.converter_version, "intelligence-event-v1")
        self.assertIn("后续情报编辑不会静默改写", document.current_revision.plain_text)
        self.assertTrue(KnowledgeSearchEntry.objects.filter(document=document).exists())
        self.assertTrue(
            SubjectKnowledgeIdentity.objects.filter(
                family=self.family,
                subject=self.subject,
                author_name=self.subject.display_name,
                is_active=True,
            ).exists()
        )
        with document.current_revision.raw_file.open("rb") as raw_file:
            snapshot = json.load(raw_file)
        self.assertEqual(snapshot["event"]["id"], event.pk)
        self.assertEqual(snapshot["event"]["summary"], event.summary)
        self.assertEqual(snapshot["evidence"][0]["canonical_url"], "https://example.com/source")

        detail = self.client.get(
            reverse("intelligence:event_detail", kwargs={"pk": event.pk})
        )
        knowledge_detail = self.client.get(
            reverse("knowledge:document_detail", kwargs={"pk": document.pk})
        )
        self.assertContains(detail, "查看已保存知识")
        self.assertContains(knowledge_detail, "返回原始情报")

    def test_repeated_archive_is_idempotent_and_can_upgrade_to_pending(self):
        event = self.make_event(title="幂等归档动态")
        self.client.force_login(self.member_user)
        archive_url = reverse("intelligence:event_archive", kwargs={"pk": event.pk})

        self.client.post(archive_url, {"mode": EventKnowledgeArchive.MODE_ARCHIVE})
        first_link = EventKnowledgeArchive.objects.get(event=event)
        first_revision_id = first_link.document.current_revision_id
        self.client.post(archive_url, {"mode": EventKnowledgeArchive.MODE_ARCHIVE})
        response = self.client.post(
            archive_url,
            {"mode": EventKnowledgeArchive.MODE_ORGANIZE},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(EventKnowledgeArchive.objects.filter(event=event).count(), 1)
        self.assertEqual(
            KnowledgeDocument.objects.filter(
                source__kind=KnowledgeSource.KIND_INTELLIGENCE,
                external_id=f"intelligence-event:{event.pk}",
            ).count(),
            1,
        )
        self.assertEqual(KnowledgeRevision.objects.filter(document=first_link.document).count(), 1)
        first_link.refresh_from_db()
        first_link.document.refresh_from_db()
        self.assertEqual(first_link.document.current_revision_id, first_revision_id)
        self.assertEqual(first_link.archive_mode, EventKnowledgeArchive.MODE_ORGANIZE)
        self.assertEqual(
            first_link.document.knowledge_status,
            KnowledgeDocument.KNOWLEDGE_PENDING,
        )
        self.assertEqual(
            first_link.document.curation_status,
            KnowledgeDocument.CURATION_INBOX,
        )

        self.client.post(
            reverse(
                "knowledge:document_cancel_organizing",
                kwargs={"pk": first_link.document.pk},
            )
        )
        self.client.post(
            archive_url,
            {"mode": EventKnowledgeArchive.MODE_ORGANIZE},
        )
        first_link.document.refresh_from_db()
        self.assertEqual(
            first_link.document.knowledge_status,
            KnowledgeDocument.KNOWLEDGE_PENDING,
        )
        self.assertEqual(KnowledgeRevision.objects.filter(document=first_link.document).count(), 1)

    def test_archive_freezes_current_ai_analysis_with_evidence_metadata(self):
        event = self.make_event(title="带 AI 分析的动态")
        provider = AiProvider.objects.create(
            name="归档测试模型",
            provider_type="openai_compatible",
            model_name="archive-test-model",
        )
        reference = f"source-item-{event.primary_source_item_id}"
        analysis = EventAnalysis.objects.create(
            event=event,
            provider=provider,
            model_name=provider.model_name,
            prompt_version="intelligence-event-v1",
            schema_version="intelligence-event-analysis-v1",
            input_fingerprint="a" * 64,
            input_snapshot={"evidence_refs": [reference]},
            result_json={
                "summary": "这是归档时采用的 AI 摘要。",
                "summary_evidence_refs": [reference],
                "why_it_matters": "这是带来源约束的影响说明。",
            },
            status=EventAnalysis.STATUS_SUCCESS,
            is_current=True,
            created_by=self.admin_user,
        )
        self.client.force_login(self.member_user)

        self.client.post(
            reverse("intelligence:event_archive", kwargs={"pk": event.pk}),
            {"mode": EventKnowledgeArchive.MODE_ARCHIVE},
        )

        link = EventKnowledgeArchive.objects.select_related(
            "document__current_revision"
        ).get(event=event)
        revision = link.document.current_revision
        self.assertEqual(revision.converter_version, "intelligence-event-v2-ai")
        self.assertIn("这是归档时采用的 AI 摘要", revision.plain_text)
        self.assertIn("不是人物原话", revision.plain_text)
        with revision.raw_file.open("rb") as raw_file:
            snapshot = json.load(raw_file)
        self.assertEqual(snapshot["ai_analysis"]["id"], analysis.pk)
        self.assertEqual(snapshot["ai_analysis"]["result"]["summary_evidence_refs"], [reference])

    def test_event_without_evidence_cannot_be_archived(self):
        event = self.make_event(title="缺少证据的动态")
        event.evidence_links.all().delete()
        self.client.force_login(self.member_user)

        response = self.client.post(
            reverse("intelligence:event_archive", kwargs={"pk": event.pk}),
            {"mode": EventKnowledgeArchive.MODE_ARCHIVE},
            follow=True,
        )

        self.assertContains(response, "还没有可核查的来源证据")
        self.assertFalse(EventKnowledgeArchive.objects.filter(event=event).exists())
        self.assertFalse(
            KnowledgeDocument.objects.filter(
                source__kind=KnowledgeSource.KIND_INTELLIGENCE,
                external_id=f"intelligence-event:{event.pk}",
            ).exists()
        )

    def test_read_only_member_cannot_archive_event(self):
        event = self.make_event(title="只读成员不可归档")
        self.client.force_login(self.viewer_user)

        detail = self.client.get(
            reverse("intelligence:event_detail", kwargs={"pk": event.pk})
        )
        response = self.client.post(
            reverse("intelligence:event_archive", kwargs={"pk": event.pk}),
            {"mode": EventKnowledgeArchive.MODE_ARCHIVE},
        )

        self.assertNotContains(detail, "保存为知识")
        self.assertEqual(response.status_code, 403)
        self.assertFalse(EventKnowledgeArchive.objects.filter(event=event).exists())

    def test_archive_snapshot_is_not_rewritten_after_event_edit(self):
        event = self.make_event(title="保留旧证据的动态")
        self.client.force_login(self.member_user)
        archive_url = reverse("intelligence:event_archive", kwargs={"pk": event.pk})
        self.client.post(archive_url, {"mode": EventKnowledgeArchive.MODE_ARCHIVE})
        link = EventKnowledgeArchive.objects.select_related(
            "document__current_revision"
        ).get(event=event)
        original_revision_id = link.document.current_revision_id
        original_text = link.document.current_revision.plain_text

        event.summary = "这是归档后才修改的情报摘要。"
        event.save(update_fields=["summary", "updated_at"])
        self.client.post(archive_url, {"mode": EventKnowledgeArchive.MODE_ARCHIVE})

        link.document.refresh_from_db()
        self.assertEqual(link.document.current_revision_id, original_revision_id)
        self.assertEqual(link.document.current_revision.plain_text, original_text)
        self.assertNotIn("归档后才修改", link.document.current_revision.plain_text)

    def test_non_owner_cannot_move_shared_archive_into_pending(self):
        event = self.make_event(title="归档所有者权限")
        archive_url = reverse("intelligence:event_archive", kwargs={"pk": event.pk})
        self.client.force_login(self.admin_user)
        self.client.post(archive_url, {"mode": EventKnowledgeArchive.MODE_ARCHIVE})

        self.client.force_login(self.member_user)
        response = self.client.post(
            archive_url,
            {"mode": EventKnowledgeArchive.MODE_ORGANIZE},
        )

        self.assertEqual(response.status_code, 403)
        link = EventKnowledgeArchive.objects.select_related("document").get(event=event)
        self.assertEqual(link.archive_mode, EventKnowledgeArchive.MODE_ARCHIVE)
        self.assertEqual(
            link.document.knowledge_status,
            KnowledgeDocument.KNOWLEDGE_INCLUDED,
        )

    def test_subject_and_knowledge_people_pages_cross_link_through_identity(self):
        event = self.make_event(title="人物跨模块链接")
        self.client.force_login(self.member_user)
        self.client.post(
            reverse("intelligence:event_archive", kwargs={"pk": event.pk}),
            {"mode": EventKnowledgeArchive.MODE_ARCHIVE},
        )

        subject_page = self.client.get(
            reverse("intelligence:subject_detail", kwargs={"slug": self.subject.slug})
        )
        knowledge_page = self.client.get(
            reverse("knowledge:people"),
            {"subject": self.subject.slug},
        )

        self.assertContains(subject_page, "历史知识（1）")
        self.assertContains(knowledge_page, "查看最新动态")
        self.assertContains(knowledge_page, event.title)


RSS_FIXTURE = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Official Test Feed</title>
  <item><guid>launch-1</guid><title>Test Person launches an AI model</title>
    <link>https://example.com/news/launch-1?utm_source=test</link>
    <description><![CDATA[The official release explains the new model and business launch.]]></description>
    <pubDate>Thu, 13 Aug 2026 01:00:00 GMT</pubDate><author>Official Team</author></item>
</channel></rss>"""

NOISE_RSS_FIXTURE = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Official Test Feed</title>
  <item><guid>lifestyle-1</guid><title>A quiet afternoon by the lake</title>
    <link>https://example.com/posts/lifestyle-1</link>
    <description>Some personal photos from the weekend.</description>
    <pubDate>Thu, 13 Aug 2026 02:00:00 GMT</pubDate></item>
</channel></rss>"""

MEDIA_EXCERPT_ONLY_FIXTURE = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Media Test Feed</title>
  <item><guid>media-excerpt-1</guid><title>A new enterprise computing plan</title>
    <link>https://example.com/media/excerpt-1</link>
    <description>OpenAI and Test Person are mentioned as background to the wider AI market.</description>
    <pubDate>Thu, 13 Aug 2026 03:00:00 GMT</pubDate></item>
</channel></rss>"""

MEDIA_TITLE_MATCH_FIXTURE = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Media Test Feed</title>
  <item><guid>media-title-1</guid><title>Test Person discusses long-term strategy</title>
    <link>https://example.com/media/title-1</link>
    <description>A reported interview about product and business priorities.</description>
    <pubDate>Thu, 13 Aug 2026 04:00:00 GMT</pubDate></item>
</channel></rss>"""

YOUTUBE_FIXTURE = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns:media="http://search.yahoo.com/mrss/">
  <title>Official Channel</title><yt:channelId>UC1234567890123456789012</yt:channelId>
  <entry><id>yt:video:video-1</id><yt:videoId>video-1</yt:videoId><title>AI keynote</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=video-1" />
    <author><name>Official Channel</name></author><published>2026-08-13T01:00:00+00:00</published>
    <media:group><media:description>Highlights from the official AI keynote.</media:description></media:group>
  </entry>
</feed>"""


class M2AdapterAndSafetyTests(IntelligenceTestBase):
    def test_rss_parser_extracts_metadata_without_full_content(self):
        items = parse_rss_or_atom(RSS_FIXTURE, base_url="https://example.com/feed.xml")

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].external_id, "launch-1")
        self.assertEqual(items[0].content_depth, SourceItem.DEPTH_DESCRIPTION)
        self.assertIn("official release", items[0].excerpt)

    def test_rdf_parser_reads_root_level_items(self):
        rdf = b"""<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"><channel><title>RDF Feed</title></channel><item><title>RDF item</title><link>https://example.com/rdf/1</link></item></rdf:RDF>"""

        items = parse_rss_or_atom(rdf, base_url="https://example.com/feed.rdf")

        self.assertEqual([item.title for item in items], ["RDF item"])

    def test_youtube_parser_only_marks_metadata_and_never_transcript(self):
        items = parse_youtube_atom(
            YOUTUBE_FIXTURE,
            expected_channel_id="UC1234567890123456789012",
        )

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].content_depth, SourceItem.DEPTH_DESCRIPTION)
        self.assertEqual(items[0].raw_metadata["transcript_status"], "not_requested")

    def test_parser_rejects_xml_entity_declarations(self):
        unsafe_xml = b'<?xml version="1.0"?><!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><rss />'

        with self.assertRaises(FeedParseError):
            parse_rss_or_atom(unsafe_xml, base_url="https://example.com/feed.xml")

    def test_public_url_validation_blocks_private_literal_addresses(self):
        with self.assertRaises(SafeHttpError) as context:
            validate_public_http_url("http://127.0.0.1/private-feed")

        self.assertEqual(context.exception.code, "private_host")
        self.assertEqual(validate_public_http_url("https://8.8.8.8/feed"), "https://8.8.8.8/feed")

    @patch("intelligence.http_client.socket.getaddrinfo")
    def test_proxy_fake_ip_requires_explicit_opt_in_and_never_allows_literal_url(self, getaddrinfo):
        getaddrinfo.return_value = [(2, 1, 6, "", ("198.18.1.154", 443))]

        with self.assertRaises(SafeHttpError):
            validate_public_http_url("https://openai.com/news/rss.xml")
        with patch.dict("os.environ", {"INTELLIGENCE_ALLOW_PROXY_FAKE_IP": "true"}):
            self.assertEqual(
                validate_public_http_url("https://openai.com/news/rss.xml"),
                "https://openai.com/news/rss.xml",
            )
            with self.assertRaises(SafeHttpError):
                validate_public_http_url("https://198.18.1.154/news/rss.xml")

    def test_source_form_rejects_private_feed_and_invalid_youtube_channel(self):
        private_form = IntelligenceSourceForm(data={
            "subject": self.subject.pk, "topics": [self.subject.pk],
            "source_type": IntelligenceSource.TYPE_RSS,
            "source_group": IntelligenceSource.GROUP_OFFICIAL,
            "adapter_key": IntelligenceSource.ADAPTER_RSS,
            "name": "Private RSS", "url": "http://127.0.0.1/feed.xml", "external_id": "",
            "source_tier": IntelligenceSource.TIER_A, "transport_weight": 100,
            "poll_interval_minutes": 60, "is_active": True,
        })
        youtube_form = IntelligenceSourceForm(data={
            "subject": self.subject.pk, "topics": [self.subject.pk],
            "source_type": IntelligenceSource.TYPE_YOUTUBE,
            "source_group": IntelligenceSource.GROUP_SOCIAL,
            "adapter_key": IntelligenceSource.ADAPTER_YOUTUBE,
            "name": "Invalid YouTube", "url": "https://www.youtube.com/@invalid", "external_id": "@invalid",
            "source_tier": IntelligenceSource.TIER_A, "transport_weight": 100,
            "poll_interval_minutes": 60, "is_active": True,
        })

        self.assertFalse(private_form.is_valid())
        self.assertIn("url", private_form.errors)
        self.assertFalse(youtube_form.is_valid())
        self.assertIn("external_id", youtube_form.errors)


class M2CollectionTests(IntelligenceTestBase):
    def make_rss_source(self, *, name="Official RSS", url="https://example.com/feed.xml"):
        source = IntelligenceSource.objects.create(
            subject=self.subject,
            source_type=IntelligenceSource.TYPE_RSS,
            adapter_key=IntelligenceSource.ADAPTER_RSS,
            name=name,
            url=url,
            source_tier=IntelligenceSource.TIER_A,
            source_group=IntelligenceSource.GROUP_OFFICIAL,
        )
        source.topics.add(self.subject)
        return source

    def make_media_source(self, *, url="https://example.com/media.xml"):
        source = IntelligenceSource.objects.create(
            subject=None,
            source_type=IntelligenceSource.TYPE_RSS,
            adapter_key=IntelligenceSource.ADAPTER_RSS,
            name="Media discovery RSS",
            url=url,
            source_tier=IntelligenceSource.TIER_C,
            source_group=IntelligenceSource.GROUP_MEDIA,
            extra_data={"discovery_policy": MEDIA_DISCOVERY_POLICY},
        )
        source.topics.add(self.subject)
        return source

    @staticmethod
    def fetch_response(body):
        return FetchResponse(
            status=200,
            url="https://example.com/feed.xml",
            body=body,
            etag='"fixture-etag"',
            last_modified="Thu, 13 Aug 2026 01:05:00 GMT",
        )

    @patch("intelligence.adapters.fetch_with_retries")
    def test_collection_is_idempotent_across_three_runs(self, fetch):
        source = self.make_rss_source()
        SubjectFollow.objects.create(family=self.family, subject=self.subject)
        fetch.return_value = self.fetch_response(RSS_FIXTURE)

        runs = [
            collect_intelligence_sources(source_ids=[source.pk], due_only=False, family=self.family)
            for _ in range(3)
        ]

        self.assertEqual(SourceItem.objects.filter(source=source).count(), 1)
        self.assertEqual(IntelligenceEvent.objects.filter(family=self.family).count(), 1)
        self.assertEqual(runs[0].created_count, 1)
        self.assertEqual([run.ignored_count for run in runs[1:]], [1, 1])
        event = IntelligenceEvent.objects.get(family=self.family)
        self.assertEqual(event.review_status, IntelligenceEvent.REVIEW_PENDING)
        self.assertEqual(event.selection_status, IntelligenceEvent.SELECTION_REVIEW)
        self.assertIn("尚未核查完整正文或视频内容", event.summary)

    @patch("intelligence.adapters.fetch_with_retries")
    def test_low_relevance_item_is_retained_as_noise_without_event(self, fetch):
        source = self.make_rss_source()
        SubjectFollow.objects.create(family=self.family, subject=self.subject)
        fetch.return_value = self.fetch_response(NOISE_RSS_FIXTURE)

        run = collect_intelligence_sources(source_ids=[source.pk], due_only=False, family=self.family)

        item = SourceItem.objects.get(source=source)
        self.assertEqual(item.processing_status, SourceItem.STATUS_NOISE)
        self.assertLess(item.relevance_score, 30)
        self.assertEqual(IntelligenceEvent.objects.count(), 0)
        self.assertEqual(run.noise_count, 1)
        self.assertEqual(run.clustered_count, 0)

    @patch("intelligence.adapters.fetch_with_retries")
    def test_media_excerpt_only_mention_is_retained_without_event(self, fetch):
        source = self.make_media_source()
        SubjectFollow.objects.create(family=self.family, subject=self.subject)
        fetch.return_value = self.fetch_response(MEDIA_EXCERPT_ONLY_FIXTURE)

        run = collect_intelligence_sources(source_ids=[source.pk], due_only=False, family=self.family)

        item = SourceItem.objects.get(source=source)
        self.assertEqual(item.processing_status, SourceItem.STATUS_NOISE)
        self.assertEqual(list(item.matched_subjects.all()), [])
        self.assertIn("媒体发现源标题未直接出现关注对象", item.processing_reason)
        self.assertEqual(IntelligenceEvent.objects.count(), 0)
        self.assertEqual(run.noise_count, 1)

    @patch("intelligence.adapters.fetch_with_retries")
    def test_media_title_mention_creates_media_candidate(self, fetch):
        source = self.make_media_source()
        SubjectFollow.objects.create(family=self.family, subject=self.subject)
        fetch.return_value = self.fetch_response(MEDIA_TITLE_MATCH_FIXTURE)

        run = collect_intelligence_sources(source_ids=[source.pk], due_only=False, family=self.family)

        item = SourceItem.objects.get(source=source)
        event = IntelligenceEvent.objects.get(family=self.family)
        self.assertEqual(item.processing_status, SourceItem.STATUS_CLUSTERED)
        self.assertEqual(list(item.matched_subjects.all()), [self.subject])
        self.assertIn("自动采集到媒体信源条目", event.summary)
        self.assertNotIn("官方信源条目", event.summary)
        self.assertEqual(run.clustered_count, 1)

    def test_one_source_failure_does_not_discard_other_source(self):
        healthy = self.make_rss_source(name="Healthy RSS", url="https://example.com/healthy.xml")
        failing = self.make_rss_source(name="Failing RSS", url="https://example.com/failing.xml")
        SubjectFollow.objects.create(family=self.family, subject=self.subject)

        class SelectiveAdapter:
            def collect(inner_self, source, *, max_items=50):
                if source.pk == failing.pk:
                    raise SafeHttpError("temporary", "信源暂时不可用。", retryable=True)
                items = parse_rss_or_atom(RSS_FIXTURE, base_url=source.url, max_items=max_items)
                return AdapterResult(items=items, cursor_updates={"fixture": True})

        with patch("intelligence.collection.get_adapter", return_value=SelectiveAdapter()):
            run = collect_intelligence_sources(
                source_ids=[healthy.pk, failing.pk], due_only=False, family=self.family
            )

        self.assertEqual(run.status, CollectionRun.STATUS_PARTIAL)
        self.assertEqual(SourceItem.objects.filter(source=healthy).count(), 1)
        self.assertEqual(run.source_results.count(), 2)
        self.assertEqual(run.failed_count, 1)
        failing.refresh_from_db()
        self.assertEqual(failing.consecutive_failures, 1)

    def test_seed_sources_is_idempotent_and_does_not_collect(self):
        call_command("seed_key_people", stdout=StringIO())
        with patch("intelligence.adapters.fetch_with_retries") as fetch:
            call_command("seed_intelligence_sources", stdout=StringIO())
            call_command("seed_intelligence_sources", stdout=StringIO())

        self.assertFalse(fetch.called)
        self.assertEqual(
            IntelligenceSource.objects.filter(adapter_key__in={"rss", "youtube"}).count(),
            len(SOURCE_DEFINITIONS),
        )
        self.assertEqual(
            IntelligenceSource.objects.filter(adapter_key="rss", is_active=True).count(),
            sum(
                definition["enabled_by_default"]
                for definition in SOURCE_DEFINITIONS
                if definition["adapter_key"] == IntelligenceSource.ADAPTER_RSS
            ),
        )
        self.assertFalse(
            IntelligenceSource.objects.filter(adapter_key="youtube", is_active=True).exists()
        )
        media_sources = IntelligenceSource.objects.filter(source_group=IntelligenceSource.GROUP_MEDIA)
        self.assertEqual(media_sources.count(), 5)
        self.assertFalse(media_sources.exclude(subject=None).exists())
        self.assertFalse(media_sources.exclude(source_tier=IntelligenceSource.TIER_C).exists())
        self.assertFalse(
            media_sources.exclude(extra_data__discovery_policy=MEDIA_DISCOVERY_POLICY).exists()
        )

    def test_collection_command_returns_nonzero_after_partial_failure(self):
        healthy = self.make_rss_source(name="Healthy command RSS", url="https://example.com/cmd-ok.xml")
        failing = self.make_rss_source(name="Failing command RSS", url="https://example.com/cmd-fail.xml")

        class SelectiveAdapter:
            def collect(inner_self, source, *, max_items=50):
                if source.pk == failing.pk:
                    raise SafeHttpError("temporary", "信源暂时不可用。")
                return AdapterResult(
                    items=parse_rss_or_atom(RSS_FIXTURE, base_url=source.url, max_items=max_items),
                    cursor_updates={},
                )

        with patch("intelligence.collection.get_adapter", return_value=SelectiveAdapter()):
            with self.assertRaises(CommandError):
                call_command(
                    "collect_intelligence_sources", "--force",
                    "--source-id", str(healthy.pk), "--source-id", str(failing.pk),
                    stdout=StringIO(), stderr=StringIO(),
                )

        self.assertEqual(CollectionRun.objects.latest("pk").status, CollectionRun.STATUS_PARTIAL)


class M2OperationsViewTests(IntelligenceTestBase):
    def make_rss_source(self):
        source = IntelligenceSource.objects.create(
            subject=self.subject,
            source_type=IntelligenceSource.TYPE_RSS,
            adapter_key=IntelligenceSource.ADAPTER_RSS,
            name="Operations RSS",
            url="https://example.com/operations.xml",
            source_tier=IntelligenceSource.TIER_A,
            source_group=IntelligenceSource.GROUP_OFFICIAL,
        )
        source.topics.add(self.subject)
        return source

    @staticmethod
    def fetch_response(body):
        return FetchResponse(status=200, url="https://example.com/operations.xml", body=body)

    @patch("intelligence.adapters.fetch_with_retries")
    def test_operations_get_never_fetches_external_sources(self, fetch):
        self.make_rss_source()
        self.client.force_login(self.admin_user)

        response = self.client.get(reverse("intelligence:operations"))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(fetch.called)
        self.assertContains(response, "普通浏览不会访问外部网站")

    @patch("intelligence.adapters.fetch_with_retries")
    def test_admin_can_explicitly_trigger_collection_post(self, fetch):
        source = self.make_rss_source()
        SubjectFollow.objects.create(family=self.family, subject=self.subject)
        fetch.return_value = self.fetch_response(RSS_FIXTURE)
        self.client.force_login(self.admin_user)

        response = self.client.post(
            reverse("intelligence:collect_sources_now"),
            {"source_id": source.pk, "force": "1"},
        )

        self.assertRedirects(response, reverse("intelligence:operations"))
        self.assertTrue(fetch.called)
        self.assertEqual(CollectionRun.objects.filter(run_kind=CollectionRun.KIND_COLLECTION).count(), 1)

    def test_non_admin_cannot_trigger_collection(self):
        self.client.force_login(self.member_user)

        response = self.client.post(reverse("intelligence:collect_sources_now"))

        self.assertEqual(response.status_code, 403)


class FakeAiResponse:
    def __init__(self, payload):
        self.body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self, _limit):
        return self.body


class M3EventAnalysisTests(IntelligenceTestBase):
    def make_provider(self):
        return AiProvider.objects.create(
            name="情报测试文本模型",
            provider_type="openai_compatible",
            base_url="https://api.example.com/v1",
            model_name="test-text-model",
            is_active=True,
            extra_data={
                "api_key_env_var": "INTELLIGENCE_TEST_API_KEY",
                "allow_intelligence_analysis": True,
                "intelligence_data_scope": "public_metadata_only",
                "intelligence_policy_version": "public-metadata-v1",
                "intelligence_policy_reviewed_on": "2026-08-16",
                "intelligence_max_input_characters": 20000,
                "intelligence_max_output_tokens": 1800,
                "intelligence_input_usd_per_million": "0.14",
                "intelligence_output_usd_per_million": "0.28",
                "intelligence_max_estimated_usd": "0.01",
                "intelligence_disable_thinking": True,
            },
        )

    @staticmethod
    def analysis_result(reference):
        return {
            "summary": "测试人物宣布新的企业级产品计划。",
            "summary_evidence_refs": [reference],
            "why_it_matters": "该计划可能影响企业软件竞争格局，但仍需观察实际采用情况。",
            "facts": [
                {"text": "来源标题显示测试人物公布了新计划。", "evidence_refs": [reference]},
            ],
            "opinions": [],
            "numbers": [],
            "uncertainties": ["公开短摘录没有提供完整发布时间表。"],
            "event_type": IntelligenceEvent.TYPE_BUSINESS,
            "change_type": IntelligenceEvent.CHANGE_NEW,
            "features": {
                "subject_relevance": 92,
                "substantiveness": 78,
                "novelty": 74,
                "potential_impact": 81,
                "investment_relevance": 63,
                "evidence_clarity": 86,
            },
        }

    def ai_payload(self, event, *, result=None):
        reference = f"source-item-{event.primary_source_item_id}"
        content = result if result is not None else self.analysis_result(reference)
        return {
            "choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 221,
                "total_tokens": 321,
            },
        }

    def test_schema_rejects_unknown_evidence_reference_and_invalid_scores(self):
        event = self.make_event(status=IntelligenceEvent.REVIEW_PENDING, selection=IntelligenceEvent.SELECTION_REVIEW)
        reference = f"source-item-{event.primary_source_item_id}"
        result = self.analysis_result(reference)
        result["facts"][0]["evidence_refs"] = ["source-item-999999"]

        with self.assertRaisesMessage(IntelligenceAiError, "不存在的来源"):
            parse_analysis_result(result, allowed_refs={reference})

        result = self.analysis_result(reference)
        result["features"]["novelty"] = 101
        with self.assertRaisesMessage(IntelligenceAiError, "超出 0 到 100"):
            parse_analysis_result(result, allowed_refs={reference})

    @patch("intelligence.ai_enrichment.urllib.request.urlopen")
    def test_existing_text_key_is_not_reused_without_intelligence_authorization(self, urlopen):
        event = self.make_event(status=IntelligenceEvent.REVIEW_PENDING, selection=IntelligenceEvent.SELECTION_REVIEW)
        provider = self.make_provider()
        provider.extra_data.pop("allow_intelligence_analysis")
        provider.save(update_fields=["extra_data", "updated_at"])

        with patch.dict("os.environ", {"INTELLIGENCE_TEST_API_KEY": "test-key"}):
            self.assertFalse(provider_is_configured(provider))
            with self.assertRaisesMessage(IntelligenceAiError, "尚未明确授权"):
                analyze_event(
                    event,
                    member=self.admin_member,
                    user=self.admin_user,
                    provider_id=provider.pk,
                )

        self.assertFalse(urlopen.called)
        self.assertFalse(EventAnalysis.objects.exists())

    @patch("intelligence.ai_enrichment.validate_public_http_url")
    @patch("intelligence.ai_enrichment.urllib.request.urlopen")
    def test_successful_analysis_is_versioned_rescored_and_idempotent(self, urlopen, validate_url):
        event = self.make_event(status=IntelligenceEvent.REVIEW_PENDING, selection=IntelligenceEvent.SELECTION_REVIEW)
        provider = self.make_provider()
        validate_url.return_value = "https://api.example.com/v1/chat/completions"
        urlopen.return_value = FakeAiResponse(self.ai_payload(event))

        with patch.dict("os.environ", {"INTELLIGENCE_TEST_API_KEY": "test-key"}):
            analysis, created = analyze_event(
                event,
                member=self.admin_member,
                user=self.admin_user,
                provider_id=provider.pk,
            )
            reused, reused_created = analyze_event(
                event,
                member=self.admin_member,
                user=self.admin_user,
                provider_id=provider.pk,
            )
            forced, forced_created = analyze_event(
                event,
                member=self.admin_member,
                user=self.admin_user,
                provider_id=provider.pk,
                force=True,
            )

        event.refresh_from_db()
        analysis.refresh_from_db()
        event.primary_source_item.refresh_from_db()
        self.assertTrue(created)
        self.assertFalse(reused_created)
        self.assertTrue(forced_created)
        self.assertEqual(reused.pk, analysis.pk)
        self.assertNotEqual(forced.pk, analysis.pk)
        self.assertEqual(urlopen.call_count, 2)
        self.assertEqual(analysis.status, EventAnalysis.STATUS_SUCCESS)
        self.assertFalse(analysis.is_current)
        self.assertTrue(forced.is_current)
        self.assertEqual(analysis.tokens_used, 321)
        self.assertEqual(analysis.cost_estimate, Decimal("0.000076"))
        self.assertEqual(analysis.result_json["summary_evidence_refs"], [f"source-item-{event.primary_source_item_id}"])
        self.assertEqual(analysis.result_json["code_scoring"]["policy_version"], "people-v1")
        self.assertEqual(event.score_origin, IntelligenceEvent.SCORE_ORIGIN_AI)
        self.assertEqual(event.relevance_score, 92)
        self.assertEqual(event.event_type, IntelligenceEvent.TYPE_BUSINESS)
        self.assertEqual(event.summary, "这是一个可核查的事实摘要。")
        self.assertEqual(event.display_summary, "测试人物宣布新的企业级产品计划。")
        self.assertEqual(event.primary_source_item.processing_status, SourceItem.STATUS_ANALYZED)
        self.assertEqual(EventAnalysis.objects.filter(event=event).count(), 2)
        self.assertEqual(EventAnalysis.objects.filter(event=event, is_current=True).count(), 1)
        self.assertEqual(AiAnalysisRequest.objects.filter(module="intelligence").count(), 2)
        self.assertEqual(AiAnalysisResult.objects.count(), 2)
        request_payload = json.loads(urlopen.call_args_list[0].args[0].data.decode("utf-8"))
        self.assertEqual(request_payload["max_tokens"], 1800)
        self.assertEqual(request_payload["thinking"], {"type": "disabled"})
        self.assertLessEqual(
            Decimal(analysis.analysis_request.scope["maximum_cost_estimate_usd"]),
            Decimal("0.01"),
        )

    @patch("intelligence.ai_enrichment.validate_public_http_url")
    @patch("intelligence.ai_enrichment.urllib.request.urlopen")
    def test_analysis_refuses_unconfirmed_public_scope_or_excessive_cost(self, urlopen, validate_url):
        event = self.make_event(status=IntelligenceEvent.REVIEW_PENDING, selection=IntelligenceEvent.SELECTION_REVIEW)
        provider = self.make_provider()
        validate_url.return_value = "https://api.example.com/v1/chat/completions"
        provider.extra_data.pop("intelligence_data_scope")
        provider.save(update_fields=["extra_data", "updated_at"])

        with patch.dict("os.environ", {"INTELLIGENCE_TEST_API_KEY": "test-key"}):
            self.assertFalse(provider_is_configured(provider))
            with self.assertRaisesMessage(IntelligenceAiError, "公开标题和短摘录"):
                analyze_event(
                    event,
                    member=self.admin_member,
                    user=self.admin_user,
                    provider_id=provider.pk,
                )

            provider.extra_data["intelligence_data_scope"] = "public_metadata_only"
            provider.extra_data["intelligence_max_estimated_usd"] = "0.000001"
            provider.save(update_fields=["extra_data", "updated_at"])
            with self.assertRaisesMessage(IntelligenceAiError, "超过单次上限"):
                analyze_event(
                    event,
                    member=self.admin_member,
                    user=self.admin_user,
                    provider_id=provider.pk,
                )

        self.assertFalse(urlopen.called)
        self.assertFalse(EventAnalysis.objects.exists())
        self.assertFalse(AiAnalysisRequest.objects.exists())

    @patch("intelligence.ai_enrichment.validate_public_http_url")
    @patch("intelligence.ai_enrichment.urllib.request.urlopen")
    def test_invalid_ai_reference_records_failure_without_mutating_event(self, urlopen, validate_url):
        event = self.make_event(status=IntelligenceEvent.REVIEW_PENDING, selection=IntelligenceEvent.SELECTION_REVIEW)
        provider = self.make_provider()
        result = self.analysis_result(f"source-item-{event.primary_source_item_id}")
        result["summary_evidence_refs"] = ["source-item-invented"]
        validate_url.return_value = "https://api.example.com/v1/chat/completions"
        urlopen.return_value = FakeAiResponse(self.ai_payload(event, result=result))

        with patch.dict("os.environ", {"INTELLIGENCE_TEST_API_KEY": "test-key"}):
            with self.assertRaisesMessage(IntelligenceAiError, "不存在的来源"):
                analyze_event(
                    event,
                    member=self.admin_member,
                    user=self.admin_user,
                    provider_id=provider.pk,
                )

        event.refresh_from_db()
        analysis = EventAnalysis.objects.get(event=event)
        self.assertEqual(analysis.status, EventAnalysis.STATUS_FAILED)
        self.assertFalse(analysis.is_current)
        self.assertEqual(event.score_origin, IntelligenceEvent.SCORE_ORIGIN_MANUAL)
        self.assertEqual(event.summary, "这是一个可核查的事实摘要。")
        self.assertEqual(AiAnalysisRequest.objects.get(pk=analysis.analysis_request_id).status, AiAnalysisRequest.STATUS_FAILED)
        self.assertFalse(AiAnalysisResult.objects.exists())

    def test_new_evidence_invalidates_current_analysis_without_deleting_history(self):
        title = "Test Person launches an AI product"
        event = self.make_event(
            status=IntelligenceEvent.REVIEW_PENDING,
            selection=IntelligenceEvent.SELECTION_REVIEW,
            title=title,
        )
        analysis = EventAnalysis.objects.create(
            event=event,
            model_name="old-model",
            prompt_version="intelligence-event-v1",
            schema_version="intelligence-event-analysis-v1",
            input_fingerprint="b" * 64,
            result_json={"summary": "旧分析"},
            status=EventAnalysis.STATUS_SUCCESS,
            is_current=True,
        )
        SubjectFollow.objects.create(family=self.family, subject=self.subject)
        new_item = SourceItem.objects.create(
            source=self.source,
            external_id="second-source-item",
            title=title,
            canonical_url="https://example.com/second-source",
            excerpt="A second source confirms the AI product launch.",
            published_at=event.occurred_at,
            content_depth=SourceItem.DEPTH_DESCRIPTION,
        )

        result = process_source_item(new_item)

        analysis.refresh_from_db()
        self.assertFalse(result.is_noise)
        self.assertFalse(analysis.is_current)
        self.assertTrue(EventAnalysis.objects.filter(pk=analysis.pk).exists())
        self.assertTrue(event.evidence_links.filter(source_item=new_item).exists())

    @patch("intelligence.ai_enrichment.validate_public_http_url")
    @patch("intelligence.ai_enrichment.urllib.request.urlopen")
    def test_admin_post_can_analyze_but_get_and_member_cannot_call_ai(self, urlopen, validate_url):
        event = self.make_event(status=IntelligenceEvent.REVIEW_PENDING, selection=IntelligenceEvent.SELECTION_REVIEW)
        provider = self.make_provider()
        validate_url.return_value = "https://api.example.com/v1/chat/completions"
        urlopen.return_value = FakeAiResponse(self.ai_payload(event))
        self.client.force_login(self.admin_user)

        with patch.dict("os.environ", {"INTELLIGENCE_TEST_API_KEY": "test-key"}):
            get_response = self.client.get(reverse("intelligence:event_detail", args=[event.pk]))
            self.assertEqual(get_response.status_code, 200)
            self.assertFalse(urlopen.called)
            post_response = self.client.post(
                reverse("intelligence:event_analyze", args=[event.pk]),
                {"provider_id": provider.pk},
            )

        self.assertRedirects(post_response, reverse("intelligence:event_detail", args=[event.pk]))
        self.assertEqual(urlopen.call_count, 1)
        detail = self.client.get(reverse("intelligence:event_detail", args=[event.pk]))
        self.assertContains(detail, "测试人物宣布新的企业级产品计划")
        self.assertContains(detail, "AI 结构化整理")
        self.assertContains(detail, "费用估算 $0.000076")
        self.assertContains(detail, f'href="#source-item-{event.primary_source_item_id}"')
        self.client.force_login(self.member_user)
        forbidden = self.client.post(
            reverse("intelligence:event_analyze", args=[event.pk]),
            {"provider_id": provider.pk},
        )
        self.assertEqual(forbidden.status_code, 403)
        self.assertEqual(urlopen.call_count, 1)
