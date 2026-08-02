import tempfile
import json
from datetime import timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from ai_analysis.models import AiAnalysisRequest, AiAnalysisResult, AiProvider
from family_core.models import Family, FamilyMember
from notes.models import InvestmentNote, InvestmentNoteType

from .content import (
    ONENOTE_CONVERTER_VERSION,
    UnsafeKnowledgeResourceError,
    resource_external_id,
    normalize_onenote_html,
    validate_resource_mime,
    validate_resource_signature,
)
from .ai import KnowledgeAiError, generate_proposals
from .crypto import decrypt_json, encrypt_json
from .microsoft import GraphResponse, MicrosoftAuthorizationError
from .models import (
    KnowledgeAsset,
    KnowledgeDocument,
    KnowledgeJob,
    KnowledgeProposal,
    KnowledgeRevision,
    KnowledgeSearchEntry,
    KnowledgeSource,
    KnowledgeVisibility,
    SourceConnection,
)
from .search import rebuild_family_search
from .services import process_job, queue_knowledge_job, recover_stale_jobs


TEST_FERNET_KEY = Fernet.generate_key().decode("ascii")


@override_settings(
    KNOWLEDGE_TOKEN_ENCRYPTION_KEY=TEST_FERNET_KEY,
    KNOWLEDGE_MICROSOFT_CLIENT_ID="test-client",
    KNOWLEDGE_MICROSOFT_CLIENT_SECRET="test-secret",
)
class KnowledgeSecurityUnitTests(SimpleTestCase):
    def test_token_payload_is_encrypted_and_round_trips(self):
        ciphertext = encrypt_json({"cache": "refresh-token-secret"})

        self.assertNotIn("refresh-token-secret", ciphertext)
        self.assertEqual(
            decrypt_json(ciphertext),
            {"cache": "refresh-token-secret"},
        )

    def test_onenote_html_keeps_safe_links_and_strips_active_content(self):
        resource_url = (
            "https://graph.microsoft.com/v1.0/me/onenote/"
            "resources/image-1/$value"
        )
        raw_html = (
            "<html><body><script>alert('x')</script>"
            '<p onclick="alert(1)">正文'
            '<a href="https://example.com/article">原文</a>'
            '<a href="javascript:alert(2)">危险</a></p>'
            f'<img src="{resource_url}" onerror="alert(3)">'
            '<img src="https://example.com/tracker.png">'
            "<iframe src=\"https://example.com\"></iframe>"
            "</body></html>"
        )

        safe_html, plain_text = normalize_onenote_html(
            raw_html,
            {resource_url: "/knowledge/assets/1/download/"},
        )

        self.assertIn("正文", plain_text)
        self.assertNotIn("alert", safe_html)
        self.assertNotIn("onclick", safe_html)
        self.assertNotIn("onerror", safe_html)
        self.assertNotIn("iframe", safe_html)
        self.assertNotIn("tracker.png", safe_html)
        self.assertIn("https://example.com/article", safe_html)
        self.assertIn("/knowledge/assets/1/download/", safe_html)

    def test_onenote_head_and_void_metadata_do_not_hide_body(self):
        raw_html = (
            "<html><head><title>不应进入正文的标题</title>"
            '<meta charset="utf-8"><link rel="stylesheet" href="bad.css">'
            "</head><body><iframe/><p>这里是原页面正文</p></body></html>"
        )

        safe_html, plain_text = normalize_onenote_html(raw_html, {})

        self.assertNotIn("不应进入正文的标题", plain_text)
        self.assertIn("这里是原页面正文", plain_text)
        self.assertNotIn("meta", safe_html)
        self.assertNotIn("iframe", safe_html)

    def test_resource_signature_rejects_spoofed_and_executable_files(self):
        with self.assertRaises(UnsafeKnowledgeResourceError):
            validate_resource_signature(b"not-a-png", "image/png")
        with self.assertRaises(UnsafeKnowledgeResourceError):
            validate_resource_signature(b"MZ executable", "application/octet-stream")
        validate_resource_signature(
            b"\x89PNG\r\n\x1a\npayload",
            "image/png",
        )

    def test_octet_stream_image_requires_a_supported_file_signature(self):
        self.assertEqual(
            validate_resource_mime(
                "application/octet-stream",
                True,
                b"\x89PNG\r\n\x1a\npayload",
            ),
            "image/png",
        )
        with self.assertRaises(UnsafeKnowledgeResourceError):
            validate_resource_mime(
                "application/octet-stream",
                True,
                b"not-an-image",
            )


@override_settings(
    KNOWLEDGE_TOKEN_ENCRYPTION_KEY=TEST_FERNET_KEY,
    KNOWLEDGE_MICROSOFT_CLIENT_ID="test-client",
    KNOWLEDGE_MICROSOFT_CLIENT_SECRET="test-secret",
)
class KnowledgeBaseTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            username="knowledge-owner",
            password="test-password",
        )
        self.other_user = User.objects.create_user(
            username="knowledge-family-member",
            password="test-password",
        )
        self.viewer_user = User.objects.create_user(
            username="knowledge-viewer",
            password="test-password",
        )
        self.family = Family.objects.create(name="知识测试家庭")
        self.member = FamilyMember.objects.create(
            family=self.family,
            user=self.user,
            display_name="资料所有者",
            role=FamilyMember.ROLE_ADMIN,
        )
        self.other_member = FamilyMember.objects.create(
            family=self.family,
            user=self.other_user,
            display_name="家庭成员",
        )
        self.viewer = FamilyMember.objects.create(
            family=self.family,
            user=self.viewer_user,
            display_name="只读成员",
            role=FamilyMember.ROLE_VIEWER,
        )
        self.other_family = Family.objects.create(name="其他家庭")
        self.outsider_user = User.objects.create_user(
            username="knowledge-outsider",
            password="test-password",
        )
        self.outsider = FamilyMember.objects.create(
            family=self.other_family,
            user=self.outsider_user,
            display_name="外部成员",
        )
        self.note_type = InvestmentNoteType.objects.get(
            code=InvestmentNoteType.CODE_RESEARCH
        )
        self.client.force_login(self.user)

        self.temp_directory = tempfile.TemporaryDirectory()
        self.storages = {
            KnowledgeRevision._meta.get_field("raw_file").storage,
            KnowledgeAsset._meta.get_field("file").storage,
        }
        self.original_locations = {}
        for storage in self.storages:
            self.original_locations[storage] = storage._location
            storage._location = self.temp_directory.name
            storage.__dict__.pop("base_location", None)
            storage.__dict__.pop("location", None)

    def tearDown(self):
        for storage, location in self.original_locations.items():
            storage._location = location
            storage.__dict__.pop("base_location", None)
            storage.__dict__.pop("location", None)
        self.temp_directory.cleanup()
        super().tearDown()

    def make_connection(self, member=None, status=SourceConnection.STATUS_ACTIVE):
        member = member or self.member
        connection = SourceConnection.objects.create(
            family=member.family,
            member=member,
            provider=SourceConnection.PROVIDER_MICROSOFT,
            external_account_id=f"account-{member.pk}",
            account_display_name=member.display_name,
            account_email=f"member-{member.pk}@example.com",
            status=status,
        )
        connection.set_token_cache(f"serialized-cache-{member.pk}")
        connection.save(update_fields=["encrypted_token_cache", "updated_at"])
        return connection

    def make_source(
        self,
        *,
        member=None,
        visibility=KnowledgeVisibility.FAMILY,
        allow_cloud_ai=False,
        suffix="default",
    ):
        member = member or self.member
        connection = self.make_connection(member)
        return KnowledgeSource.objects.create(
            family=member.family,
            owner=member,
            connection=connection,
            key=f"onenote:{member.pk}:{suffix}",
            kind=KnowledgeSource.KIND_ONENOTE,
            name=f"测试笔记本 {suffix}",
            external_id=f"notebook-{suffix}",
            visibility=visibility,
            allow_cloud_ai=allow_cloud_ai,
        )

    def make_document(
        self,
        *,
        source=None,
        owner=None,
        visibility=KnowledgeVisibility.FAMILY,
        title="知识文档",
        external_id="page-1",
        normalized_html="<p>安全正文</p>",
        plain_text="安全正文",
    ):
        source = source or self.make_source()
        owner = owner or source.owner
        document = KnowledgeDocument.objects.create(
            family=source.family,
            source=source,
            owner=owner,
            external_id=external_id,
            title=title,
            visibility=visibility,
            content_modified_at=timezone.now(),
            curation_status=KnowledgeDocument.CURATION_NORMALIZED,
        )
        revision = KnowledgeRevision.objects.create(
            document=document,
            revision_number=1,
            content_hash=(external_id.encode("utf-8").hex() + "0" * 64)[:64],
            raw_file="",
            normalized_html=normalized_html,
            plain_text=plain_text,
        )
        revision.raw_file.save(
            "page.html",
            ContentFile(normalized_html.encode("utf-8")),
            save=True,
        )
        document.current_revision = revision
        document.save(update_fields=["current_revision", "updated_at"])
        document.refresh_from_db()
        return document

    def test_existing_note_projection_updates_without_creating_editable_copy(self):
        own_note = InvestmentNote.objects.create(
            family=self.family,
            member=self.member,
            title="自己的私密灵感",
            content="关于风险控制的想法",
            note_type=self.note_type,
            visibility=InvestmentNote.VISIBILITY_PRIVATE,
            tags=["风险控制"],
        )
        shared_note = InvestmentNote.objects.create(
            family=self.family,
            member=self.other_member,
            title="家庭共享灵感",
            content="家庭都可以看到",
            note_type=self.note_type,
            visibility=InvestmentNote.VISIBILITY_FAMILY,
        )
        hidden_note = InvestmentNote.objects.create(
            family=self.family,
            member=self.other_member,
            title="其他成员私密灵感",
            content="不可泄露",
            note_type=self.note_type,
            visibility=InvestmentNote.VISIBILITY_PRIVATE,
        )

        response = self.client.get(
            reverse("knowledge:library"),
            {"q": "灵感", "member": "all"},
        )

        self.assertContains(response, own_note.title)
        self.assertContains(response, shared_note.title)
        self.assertNotContains(response, hidden_note.title)
        self.assertEqual(
            KnowledgeSearchEntry.objects.filter(
                item_kind=KnowledgeSearchEntry.KIND_INVESTMENT_NOTE
            ).count(),
            3,
        )
        self.assertEqual(InvestmentNote.objects.count(), 3)

        own_note.title = "修改后的灵感"
        own_note.save()
        entry = KnowledgeSearchEntry.objects.get(
            item_kind=KnowledgeSearchEntry.KIND_INVESTMENT_NOTE,
            object_id=str(own_note.pk),
        )
        self.assertEqual(entry.title, "修改后的灵感")
        own_note.delete()
        self.assertFalse(
            KnowledgeSearchEntry.objects.filter(
                item_kind=KnowledgeSearchEntry.KIND_INVESTMENT_NOTE,
                object_id=str(own_note.pk),
            ).exists()
        )

    def test_private_document_and_search_snippet_do_not_leak(self):
        private_source = self.make_source(
            member=self.other_member,
            visibility=KnowledgeVisibility.PRIVATE,
            suffix="private",
        )
        private_document = self.make_document(
            source=private_source,
            owner=self.other_member,
            visibility=KnowledgeVisibility.PRIVATE,
            title="绝密家庭资料",
            external_id="private-page",
            plain_text="不可泄露关键词",
        )
        family_document = self.make_document(
            title="家庭可见资料",
            external_id="family-page",
        )

        response = self.client.get(
            reverse("knowledge:library"),
            {"q": "资料"},
        )

        self.assertContains(response, family_document.title)
        self.assertNotContains(response, private_document.title)
        self.assertEqual(
            self.client.get(
                reverse(
                    "knowledge:document_detail",
                    kwargs={"pk": private_document.pk},
                )
            ).status_code,
            404,
        )

        self.client.force_login(self.other_user)
        owner_response = self.client.get(
            reverse(
                "knowledge:document_detail",
                kwargs={"pk": private_document.pk},
            )
        )
        self.assertEqual(owner_response.status_code, 200)
        self.assertContains(owner_response, private_document.title)

    def test_knowledge_hub_exposes_daily_workflows_and_planning_boundaries(self):
        document = self.make_document(
            title="英伟达研究摘录",
            external_id="hub-page",
            plain_text="关于半导体周期与估值的原文",
        )
        document.tags = ["半导体", "估值"]
        document.category = "投资研究"
        document.author = "黄仁勋"
        document.save(
            update_fields=["tags", "category", "author", "updated_at"]
        )
        InvestmentNote.objects.create(
            family=self.family,
            member=self.member,
            title="今日灵感",
            content="记录一个待验证的投资想法",
            note_type=self.note_type,
            visibility=InvestmentNote.VISIBILITY_PRIVATE,
            tags=["灵感"],
        )

        home = self.client.get(reverse("knowledge:index"))
        self.assertContains(home, "今天需要做什么？")
        self.assertContains(home, "动态收件箱")
        self.assertContains(home, "在线阅读")
        self.assertContains(home, "交易复盘")
        self.assertContains(home, document.title)

        inbox = self.client.get(reverse("knowledge:inbox"))
        self.assertContains(inbox, document.title)
        self.assertContains(inbox, "AI 建议")

        library = self.client.get(
            reverse("knowledge:library"),
            {"q": "半导体"},
        )
        self.assertContains(library, document.title)

        topics = self.client.get(reverse("knowledge:topics"))
        self.assertContains(topics, "#半导体")
        self.assertContains(topics, "投资研究")

        architecture = self.client.get(reverse("knowledge:architecture"))
        self.assertContains(architecture, "模块化 Django 单体")
        self.assertContains(architecture, "knowledge/connectors")
        self.assertContains(architecture, "ai_analysis")

        people = self.client.get(reverse("knowledge:people"))
        self.assertContains(people, "关注人物")
        self.assertContains(people, "黄仁勋")
        person_timeline = self.client.get(
            reverse("knowledge:people"),
            {"person": "黄仁勋"},
        )
        self.assertContains(person_timeline, document.title)
        self.assertContains(person_timeline, "身份待核验")

    def test_library_defaults_to_current_member_and_can_switch_to_all_members(self):
        private_document = self.make_document(
            visibility=KnowledgeVisibility.PRIVATE,
            title="我的私密资料",
            external_id="my-private-page",
        )
        family_document = self.make_document(
            source=private_document.source,
            visibility=KnowledgeVisibility.FAMILY,
            title="家庭共享资料",
            external_id="family-shared-page",
        )
        other_source = self.make_source(
            member=self.other_member,
            visibility=KnowledgeVisibility.FAMILY,
            suffix="other-shared",
        )
        other_family_document = self.make_document(
            source=other_source,
            owner=self.other_member,
            visibility=KnowledgeVisibility.FAMILY,
            title="其他成员共享资料",
            external_id="other-family-shared-page",
        )

        default_library = self.client.get(reverse("knowledge:library"))
        self.assertContains(default_library, private_document.title)
        self.assertContains(default_library, family_document.title)
        self.assertNotContains(default_library, other_family_document.title)
        self.assertContains(default_library, "默认显示当前登录成员")

        all_members = self.client.get(
            reverse("knowledge:library"),
            {"member": "all"},
        )
        self.assertContains(all_members, private_document.title)
        self.assertContains(all_members, family_document.title)
        self.assertContains(all_members, other_family_document.title)

        self.assertRedirects(
            self.client.get(reverse("knowledge:personal_library")),
            reverse("knowledge:library"),
        )
        self.assertRedirects(
            self.client.get(reverse("knowledge:family_library")),
            reverse("knowledge:library") + "?member=all",
        )

    def test_source_selection_defaults_to_family_visibility_without_syncing(self):
        connection = self.make_connection()
        connection.available_notebooks = [
            {
                "id": "pilot-notebook",
                "displayName": "家庭知识试点",
                "webUrl": "https://www.onenote.com/notebook/pilot",
            }
        ]
        connection.save(update_fields=["available_notebooks", "updated_at"])

        response = self.client.post(
            reverse("knowledge:notebook_select"),
            {
                "notebook_id": "pilot-notebook",
                "visibility": KnowledgeVisibility.FAMILY,
            },
        )

        source = KnowledgeSource.objects.get(external_id="pilot-notebook")
        self.assertRedirects(
            response,
            reverse("knowledge:source_detail", kwargs={"pk": source.pk}),
        )
        self.assertEqual(source.owner, self.member)
        self.assertEqual(source.visibility, KnowledgeVisibility.FAMILY)
        self.assertFalse(source.allow_cloud_ai)
        self.assertFalse(KnowledgeJob.objects.exists())

    @patch("knowledge.views.MicrosoftGraphClient")
    @patch("knowledge.views.finish_authorization_flow")
    def test_oauth_callback_stores_separate_encrypted_member_cache(
        self,
        finish_authorization,
        graph_client_class,
    ):
        finish_authorization.return_value = (
            {
                "access_token": "not-persisted-access-token",
                "scope": "Notes.Read User.Read",
                "id_token_claims": {
                    "oid": "member-account-id",
                    "name": "测试账户",
                    "preferred_username": "member@example.com",
                },
            },
            "serialized-msal-cache-with-refresh-token",
        )
        graph = graph_client_class.return_value
        graph.profile.return_value = {
            "id": "member-account-id",
            "displayName": "测试账户",
            "mail": "member@example.com",
        }
        graph.notebooks.return_value = [
            {"id": "notebook-1", "displayName": "试点笔记本", "links": {}}
        ]
        session = self.client.session
        session["knowledge_microsoft_flow"] = {"state": "expected-state"}
        session["knowledge_microsoft_member_id"] = self.member.pk
        session.save()

        response = self.client.get(
            reverse("knowledge:microsoft_callback"),
            {"code": "authorization-code", "state": "expected-state"},
        )

        self.assertRedirects(response, reverse("knowledge:sources"))
        connection = SourceConnection.objects.get(member=self.member)
        self.assertNotIn(
            "refresh-token",
            connection.encrypted_token_cache,
        )
        self.assertEqual(
            connection.get_token_cache(),
            "serialized-msal-cache-with-refresh-token",
        )
        self.assertEqual(connection.available_notebooks[0]["id"], "notebook-1")
        sources_response = self.client.get(reverse("knowledge:sources"))
        self.assertNotContains(
            sources_response,
            "serialized-msal-cache-with-refresh-token",
        )

    def test_job_queue_is_idempotent_and_web_request_does_not_run_sync(self):
        source = self.make_source()
        with patch("knowledge.services.MicrosoftGraphClient") as graph_client:
            first = self.client.post(
                reverse("knowledge:source_sync", kwargs={"pk": source.pk})
            )
            second = self.client.post(
                reverse("knowledge:source_sync", kwargs={"pk": source.pk})
            )

        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 302)
        self.assertEqual(
            KnowledgeJob.objects.filter(
                source=source,
                job_type=KnowledgeJob.TYPE_SYNC_SOURCE,
            ).count(),
            1,
        )
        self.assertEqual(
            KnowledgeJob.objects.get(source=source).status,
            KnowledgeJob.STATUS_PENDING,
        )
        graph_client.assert_not_called()

    def test_sync_is_incremental_versioned_sanitized_and_reconciles_deletion(self):
        source = self.make_source(suffix="sync")
        image_url = (
            "https://graph.microsoft.com/v1.0/me/onenote/"
            "resources/image-1/$value"
        )

        class FakeGraphClient:
            def __init__(self):
                self.modified = "2026-07-30T01:00:00Z"
                self.page_ids = ["page-1", "page-2"]
                self.html = {
                    "page-1": (
                        "<html><body><script>alert(1)</script>"
                        '<p>第一版 <a href="https://example.com/article">原文链接</a></p>'
                        f'<img src="{image_url}" data-src-type="image/png">'
                        "</body></html>"
                    ),
                    "page-2": "<html><body><p>第二页</p></body></html>",
                }

            def sections_for_notebook(self, notebook_id):
                return [
                    {
                        "id": "section-1",
                        "displayName": "财经资料",
                        "parentSectionGroup": {"displayName": "研究"},
                    }
                ]

            def pages_for_section(self, section_id):
                return [
                    {
                        "id": page_id,
                        "title": f"页面 {page_id}",
                        "createdDateTime": "2026-07-29T01:00:00Z",
                        "lastModifiedDateTime": (
                            self.modified
                            if page_id == "page-1"
                            else "2026-07-30T02:00:00Z"
                        ),
                        "links": {
                            "oneNoteWebUrl": {
                                "href": f"https://www.onenote.com/page/{page_id}"
                            }
                        },
                        "level": 0,
                        "order": 1,
                    }
                    for page_id in self.page_ids
                ]

            def page_content(self, page_id):
                return self.html[page_id].encode("utf-8")

            def resource(self, url):
                return GraphResponse(
                    body=b"\x89PNG\r\n\x1a\n" + b"image-data",
                    content_type="application/octet-stream",
                    content_disposition="",
                )

        fake = FakeGraphClient()

        def run_sync(full_reconcile=False):
            job = KnowledgeJob.objects.create(
                family=self.family,
                source=source,
                requested_by=self.member,
                job_type=KnowledgeJob.TYPE_SYNC_SOURCE,
                parameters={"full_reconcile": full_reconcile},
            )
            with patch("knowledge.services.MicrosoftGraphClient", return_value=fake):
                process_job(job)
            job.refresh_from_db()
            return job

        first = run_sync()
        self.assertEqual(first.status, KnowledgeJob.STATUS_SUCCESS)
        self.assertEqual(first.success_count, 2)
        self.assertEqual(source.documents.count(), 2)
        page_one = source.documents.get(external_id="page-1")
        self.assertEqual(page_one.revisions.count(), 1)
        self.assertEqual(page_one.current_revision.assets.count(), 1)
        self.assertEqual(
            page_one.current_revision.assets.get().mime_type,
            "image/png",
        )
        self.assertEqual(
            page_one.current_revision.converter_version,
            ONENOTE_CONVERTER_VERSION,
        )
        self.assertNotIn("script", page_one.current_revision.normalized_html)
        self.assertNotIn("alert", page_one.current_revision.normalized_html)
        self.assertIn("https://example.com/article", page_one.current_revision.normalized_html)
        self.assertIn("/knowledge/assets/", page_one.current_revision.normalized_html)
        self.assertTrue(page_one.current_revision.raw_file.storage.exists(
            page_one.current_revision.raw_file.name
        ))

        outdated_revision = page_one.current_revision
        outdated_revision.normalized_html = "<p>只剩标题</p>"
        outdated_revision.plain_text = "只剩标题"
        outdated_revision.converter_version = "onenote-html-v1"
        outdated_revision.save(
            update_fields=["normalized_html", "plain_text", "converter_version"]
        )
        reprocessed = run_sync()
        self.assertEqual(reprocessed.updated_count, 1)
        self.assertEqual(reprocessed.skipped_count, 1)
        page_one.refresh_from_db()
        self.assertEqual(page_one.revisions.count(), 1)
        self.assertIn("第一版", page_one.current_revision.plain_text)
        self.assertEqual(
            page_one.current_revision.converter_version,
            ONENOTE_CONVERTER_VERSION,
        )

        second = run_sync()
        self.assertEqual(second.status, KnowledgeJob.STATUS_SUCCESS)
        self.assertEqual(second.skipped_count, 2)
        page_one.refresh_from_db()
        self.assertEqual(page_one.revisions.count(), 1)

        fake.modified = "2026-07-30T03:00:00Z"
        fake.html["page-1"] = fake.html["page-1"].replace("第一版", "第二版")
        changed = run_sync()
        self.assertEqual(changed.updated_count, 1)
        page_one.refresh_from_db()
        self.assertEqual(page_one.revisions.count(), 2)
        self.assertIn("第二版", page_one.current_revision.plain_text)

        fake.page_ids = ["page-1"]
        reconciled = run_sync(full_reconcile=True)
        self.assertEqual(reconciled.status, KnowledgeJob.STATUS_SUCCESS)
        page_two = source.documents.get(external_id="page-2")
        self.assertEqual(
            page_two.sync_status,
            KnowledgeDocument.SYNC_SOURCE_DELETED,
        )
        self.assertIsNotNone(page_two.source_deleted_at)
        self.assertEqual(reconciled.result["source_deleted_marked"], 1)

    def test_rebuild_content_uses_immutable_raw_file_and_saved_assets(self):
        source = self.make_source(suffix="rebuild-content")
        document = self.make_document(
            source=source,
            external_id="rebuild-page",
            title="德勤",
            normalized_html="<p>德勤</p>",
            plain_text="德勤",
        )
        revision = document.current_revision
        image_url = (
            "https://graph.microsoft.com/v1.0/me/onenote/"
            "resources/rebuild-image/$value"
        )
        raw_html = (
            "<html><head><title>德勤</title><meta charset=\"utf-8\"></head>"
            "<body><p>完整的原页面正文</p>"
            f'<img src="{image_url}" data-src-type="image/png"></body></html>'
        ).encode("utf-8")
        revision.raw_file.delete(save=False)
        revision.raw_file.save("page.html", ContentFile(raw_html), save=True)
        asset = KnowledgeAsset.objects.create(
            revision=revision,
            external_id=resource_external_id(image_url),
            original_name="rebuild.png",
            mime_type="image/png",
            byte_size=12,
            content_hash="b" * 64,
            is_image=True,
            file="",
        )
        asset.file.save(
            "rebuild.png",
            ContentFile(b"\x89PNG\r\n\x1a\nbody"),
            save=True,
        )
        raw_name = revision.raw_file.name
        asset_name = asset.file.name
        preview_output = StringIO()

        call_command(
            "rebuild_knowledge_content",
            source_id=source.pk,
            dry_run=True,
            stdout=preview_output,
        )

        revision.refresh_from_db()
        self.assertEqual(revision.converter_version, "onenote-html-v1")
        self.assertEqual(revision.plain_text, "德勤")
        self.assertIn(
            "previewed updated=1 skipped=0 failed=0",
            preview_output.getvalue(),
        )
        output = StringIO()

        call_command(
            "rebuild_knowledge_content",
            source_id=source.pk,
            stdout=output,
        )

        revision.refresh_from_db()
        self.assertEqual(revision.raw_file.name, raw_name)
        with revision.raw_file.open("rb") as raw_file:
            self.assertEqual(raw_file.read(), raw_html)
        self.assertEqual(revision.revision_number, 1)
        self.assertEqual(
            revision.converter_version,
            ONENOTE_CONVERTER_VERSION,
        )
        self.assertIn("完整的原页面正文", revision.plain_text)
        self.assertIn(
            reverse("knowledge:asset_download", kwargs={"pk": asset.pk}),
            revision.normalized_html,
        )
        asset.refresh_from_db()
        self.assertEqual(asset.file.name, asset_name)
        with asset.file.open("rb") as asset_file:
            self.assertEqual(asset_file.read(), b"\x89PNG\r\n\x1a\nbody")
        search_entry = KnowledgeSearchEntry.objects.get(
            item_kind=KnowledgeSearchEntry.KIND_DOCUMENT,
            object_id=str(document.pk),
        )
        self.assertIn("完整的原页面正文", search_entry.body)
        self.assertIn("rebuilt updated=1 skipped=0 failed=0", output.getvalue())

        second_output = StringIO()
        call_command(
            "rebuild_knowledge_content",
            source_id=source.pk,
            stdout=second_output,
        )
        self.assertIn(
            "rebuilt updated=0 skipped=1 failed=0",
            second_output.getvalue(),
        )

    def test_authorization_failure_marks_job_source_unavailable_without_deleting_data(self):
        source = self.make_source(suffix="auth-failure")
        existing_document = self.make_document(
            source=source,
            external_id="existing",
        )
        job = KnowledgeJob.objects.create(
            family=self.family,
            source=source,
            requested_by=self.member,
            job_type=KnowledgeJob.TYPE_SYNC_SOURCE,
        )
        with patch(
            "knowledge.services.MicrosoftGraphClient",
            side_effect=MicrosoftAuthorizationError("授权已撤销"),
        ):
            process_job(job)

        job.refresh_from_db()
        existing_document.refresh_from_db()
        self.assertEqual(job.status, KnowledgeJob.STATUS_SOURCE_UNAVAILABLE)
        self.assertTrue(KnowledgeDocument.objects.filter(pk=existing_document.pk).exists())
        self.assertEqual(
            existing_document.sync_status,
            KnowledgeDocument.SYNC_AVAILABLE,
        )

    def test_proposals_require_owner_review_and_bulk_has_preview_step(self):
        source = self.make_source(suffix="proposal", allow_cloud_ai=True)
        document = self.make_document(
            source=source,
            external_id="proposal-page",
            title="需要整理的文档",
        )
        proposals = {}
        suggested = {
            KnowledgeProposal.TYPE_SUMMARY: {"text": "AI 摘要"},
            KnowledgeProposal.TYPE_TAGS: {"items": ["投资", "风险"]},
            KnowledgeProposal.TYPE_CATEGORY: {"value": "投资"},
        }
        for proposal_type, value in suggested.items():
            proposals[proposal_type] = KnowledgeProposal.objects.create(
                document=document,
                revision=document.current_revision,
                proposal_type=proposal_type,
                suggested_value=value,
                model_name="test-model",
                prompt_version="knowledge-organize-v1",
                content_hash=document.current_revision.content_hash,
            )
        document.curation_status = KnowledgeDocument.CURATION_PENDING_REVIEW
        document.save(update_fields=["curation_status", "updated_at"])

        self.client.force_login(self.other_user)
        denied = self.client.post(
            reverse(
                "knowledge:proposal_review",
                kwargs={"pk": proposals[KnowledgeProposal.TYPE_SUMMARY].pk},
            ),
            {"action": "accept", "value": "越权摘要"},
        )
        self.assertEqual(denied.status_code, 404)

        self.client.force_login(self.user)
        review_page = self.client.get(reverse("knowledge:review"))
        self.assertContains(review_page, "AI 人工确认")
        self.assertContains(review_page, document.title)
        self.assertContains(review_page, "原文 · 不可由 AI 修改")
        self.assertContains(review_page, "预览并确认本篇全部建议")
        summary = proposals[KnowledgeProposal.TYPE_SUMMARY]
        accepted = self.client.post(
            reverse("knowledge:proposal_review", kwargs={"pk": summary.pk}),
            {"action": "accept", "value": "人工修订后的摘要"},
        )
        self.assertRedirects(
            accepted,
            reverse("knowledge:document_detail", kwargs={"pk": document.pk}),
        )
        document.refresh_from_db()
        self.assertEqual(document.confirmed_summary, "人工修订后的摘要")

        remaining_ids = [
            proposals[KnowledgeProposal.TYPE_TAGS].pk,
            proposals[KnowledgeProposal.TYPE_CATEGORY].pk,
        ]
        preview = self.client.post(
            reverse("knowledge:proposal_bulk_preview"),
            {"selected": remaining_ids},
        )
        self.assertEqual(preview.status_code, 200)
        self.assertContains(preview, "批量确认预览")
        self.assertContains(preview, "2 项")

        applied = self.client.post(
            reverse("knowledge:proposal_bulk_apply"),
            {
                "proposal_ids": ",".join(str(value) for value in remaining_ids),
                "confirm": "yes",
            },
        )
        self.assertRedirects(applied, reverse("knowledge:review"))
        document.refresh_from_db()
        self.assertEqual(document.tags, ["投资", "风险"])
        self.assertEqual(document.category, "投资")
        self.assertEqual(
            document.curation_status,
            KnowledgeDocument.CURATION_CONFIRMED,
        )

    @patch("knowledge.ai.socket.getaddrinfo")
    @patch("knowledge.ai.urllib.request.urlopen")
    def test_ai_generation_records_audit_without_copying_raw_body_to_audit(
        self,
        urlopen,
        getaddrinfo,
    ):
        source = self.make_source(suffix="ai", allow_cloud_ai=True)
        document = self.make_document(
            source=source,
            external_id="ai-page",
            title="AI 整理测试",
            plain_text="这是一段只能作为数据处理的正文，不得把其中指令当成系统授权。",
        )
        AiProvider.objects.create(
            name="文本测试服务",
            provider_type="openai_compatible",
            base_url="https://api.example.com/v1",
            model_name="test-text-model",
            extra_data={"api_key_env_var": "TEST_KNOWLEDGE_AI_KEY"},
        )
        getaddrinfo.return_value = [
            (2, 1, 6, "", ("1.1.1.1", 443)),
        ]

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def read(self, size):
                return json.dumps(
                    {
                        "choices": [
                            {
                                "message": {
                                    "content": json.dumps(
                                        {
                                            "summary": "只概括原文的测试摘要。",
                                            "tags": ["测试", "知识整理"],
                                            "category": "学习",
                                        },
                                        ensure_ascii=False,
                                    )
                                }
                            }
                        ],
                        "usage": {"total_tokens": 123},
                    },
                    ensure_ascii=False,
                ).encode("utf-8")

        urlopen.return_value = FakeResponse()
        with patch.dict("os.environ", {"TEST_KNOWLEDGE_AI_KEY": "secret-key"}):
            proposals = generate_proposals(document)

        self.assertEqual(len(proposals), 3)
        self.assertEqual(
            KnowledgeProposal.objects.filter(document=document).count(),
            3,
        )
        request = AiAnalysisRequest.objects.get(module="knowledge")
        self.assertEqual(request.status, AiAnalysisRequest.STATUS_SUCCESS)
        self.assertNotIn(document.current_revision.plain_text, str(request.sanitized_input))
        self.assertEqual(request.scope["content_hash"], document.current_revision.content_hash)
        self.assertEqual(AiAnalysisResult.objects.get(request=request).tokens_used, 123)
        document.refresh_from_db()
        self.assertEqual(
            document.curation_status,
            KnowledgeDocument.CURATION_PENDING_REVIEW,
        )

    def test_ai_generation_requires_explicit_source_consent(self):
        source = self.make_source(suffix="ai-no-consent", allow_cloud_ai=False)
        document = self.make_document(
            source=source,
            external_id="ai-no-consent-page",
        )

        with self.assertRaisesMessage(
            KnowledgeAiError,
            "未授权",
        ):
            generate_proposals(document)

        self.assertFalse(AiAnalysisRequest.objects.exists())
        self.assertFalse(KnowledgeProposal.objects.exists())

    def test_protected_asset_requires_current_document_permission(self):
        source = self.make_source(
            visibility=KnowledgeVisibility.PRIVATE,
            suffix="asset",
        )
        document = self.make_document(
            source=source,
            visibility=KnowledgeVisibility.PRIVATE,
            external_id="asset-page",
        )
        asset = KnowledgeAsset.objects.create(
            revision=document.current_revision,
            external_id="asset-1",
            original_name="image.png",
            mime_type="image/png",
            byte_size=12,
            content_hash="a" * 64,
            is_image=True,
            file="",
        )
        asset.file.save(
            "image.png",
            ContentFile(b"\x89PNG\r\n\x1a\nbody"),
            save=True,
        )
        url = reverse("knowledge:asset_download", kwargs={"pk": asset.pk})

        owner_response = self.client.get(url)
        self.assertEqual(owner_response.status_code, 200)
        self.assertEqual(owner_response.headers["Cache-Control"], "private, no-store")

        self.client.force_login(self.other_user)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.client.force_login(self.outsider_user)
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_source_default_change_does_not_silently_widen_existing_document(self):
        source = self.make_source(
            visibility=KnowledgeVisibility.PRIVATE,
            suffix="visibility",
        )
        document = self.make_document(
            source=source,
            visibility=KnowledgeVisibility.PRIVATE,
            external_id="private-existing",
        )

        response = self.client.post(
            reverse("knowledge:source_update", kwargs={"pk": source.pk}),
            {"visibility": KnowledgeVisibility.FAMILY},
        )

        self.assertRedirects(
            response,
            reverse("knowledge:source_detail", kwargs={"pk": source.pk}),
        )
        source.refresh_from_db()
        document.refresh_from_db()
        self.assertEqual(source.visibility, KnowledgeVisibility.FAMILY)
        self.assertEqual(document.visibility, KnowledgeVisibility.PRIVATE)

    def test_family_admin_cannot_grant_cloud_ai_consent_for_another_member(self):
        source = self.make_source(
            member=self.other_member,
            visibility=KnowledgeVisibility.PRIVATE,
            suffix="owner-consent",
        )

        response = self.client.post(
            reverse("knowledge:source_update", kwargs={"pk": source.pk}),
            {
                "visibility": KnowledgeVisibility.FAMILY,
                "allow_cloud_ai": "on",
            },
        )

        self.assertEqual(response.status_code, 403)
        source.refresh_from_db()
        self.assertEqual(source.visibility, KnowledgeVisibility.PRIVATE)
        self.assertFalse(source.allow_cloud_ai)

    def test_viewer_can_browse_but_cannot_create_tasks_or_confirm(self):
        source = self.make_source(suffix="viewer")
        document = self.make_document(source=source, external_id="viewer-page")
        proposal = KnowledgeProposal.objects.create(
            document=document,
            revision=document.current_revision,
            proposal_type=KnowledgeProposal.TYPE_SUMMARY,
            suggested_value={"text": "摘要"},
            model_name="test-model",
            prompt_version="knowledge-organize-v1",
            content_hash=document.current_revision.content_hash,
        )
        self.client.force_login(self.viewer_user)

        self.assertEqual(self.client.get(reverse("knowledge:index")).status_code, 200)
        self.assertEqual(
            self.client.post(
                reverse("knowledge:source_sync", kwargs={"pk": source.pk})
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                reverse("knowledge:proposal_review", kwargs={"pk": proposal.pk}),
                {"action": "accept", "value": "摘要"},
            ).status_code,
            403,
        )

    def test_rebuild_search_projection_is_deterministic(self):
        note = InvestmentNote.objects.create(
            family=self.family,
            member=self.member,
            title="重建索引笔记",
            content="原始业务正文",
            note_type=self.note_type,
            visibility=InvestmentNote.VISIBILITY_PRIVATE,
        )
        document = self.make_document(
            external_id="rebuild-page",
            title="重建索引文档",
        )
        KnowledgeSearchEntry.objects.filter(family=self.family).delete()

        first = rebuild_family_search(self.family)
        first_rows = list(
            KnowledgeSearchEntry.objects.filter(family=self.family)
            .values_list("item_kind", "object_id", "title")
            .order_by("item_kind", "object_id")
        )
        second = rebuild_family_search(self.family)
        second_rows = list(
            KnowledgeSearchEntry.objects.filter(family=self.family)
            .values_list("item_kind", "object_id", "title")
            .order_by("item_kind", "object_id")
        )

        self.assertEqual(first, {"notes": 1, "documents": 1})
        self.assertEqual(second, first)
        self.assertEqual(first_rows, second_rows)
        self.assertIn(
            (
                KnowledgeSearchEntry.KIND_INVESTMENT_NOTE,
                str(note.pk),
                note.title,
            ),
            first_rows,
        )
        self.assertIn(
            (
                KnowledgeSearchEntry.KIND_DOCUMENT,
                str(document.pk),
                document.title,
            ),
            first_rows,
        )

    def test_stale_running_job_is_not_duplicated_by_queue(self):
        source = self.make_source(suffix="active-job")
        existing = KnowledgeJob.objects.create(
            family=self.family,
            source=source,
            requested_by=self.member,
            job_type=KnowledgeJob.TYPE_SYNC_SOURCE,
            status=KnowledgeJob.STATUS_RUNNING,
            heartbeat_at=timezone.now() - timedelta(minutes=5),
        )

        queued, created = queue_knowledge_job(
            family=self.family,
            source=source,
            requested_by=self.member,
            job_type=KnowledgeJob.TYPE_SYNC_SOURCE,
        )

        self.assertFalse(created)
        self.assertEqual(queued.pk, existing.pk)

    def test_stale_running_job_is_failed_and_can_be_retried(self):
        source = self.make_source(suffix="stale-job")
        stale = KnowledgeJob.objects.create(
            family=self.family,
            source=source,
            requested_by=self.member,
            job_type=KnowledgeJob.TYPE_SYNC_SOURCE,
            status=KnowledgeJob.STATUS_RUNNING,
            started_at=timezone.now() - timedelta(hours=1),
            heartbeat_at=timezone.now() - timedelta(hours=1),
        )

        self.assertEqual(recover_stale_jobs(), 1)
        stale.refresh_from_db()
        self.assertEqual(stale.status, KnowledgeJob.STATUS_FAILED)
        self.assertIsNotNone(stale.finished_at)

        retried, created = queue_knowledge_job(
            family=self.family,
            source=source,
            requested_by=self.member,
            job_type=KnowledgeJob.TYPE_SYNC_SOURCE,
        )
        self.assertTrue(created)
        self.assertNotEqual(retried.pk, stale.pk)

    def test_pending_job_can_be_cancelled_immediately(self):
        source = self.make_source(suffix="cancel-job")
        job = KnowledgeJob.objects.create(
            family=self.family,
            source=source,
            requested_by=self.member,
            job_type=KnowledgeJob.TYPE_SYNC_SOURCE,
        )

        response = self.client.post(
            reverse("knowledge:job_cancel", kwargs={"pk": job.pk})
        )

        self.assertRedirects(
            response,
            reverse("knowledge:job_detail", kwargs={"pk": job.pk}),
        )
        job.refresh_from_db()
        self.assertEqual(job.status, KnowledgeJob.STATUS_CANCELLED)
        self.assertIsNotNone(job.finished_at)
