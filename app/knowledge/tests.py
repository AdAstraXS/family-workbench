import tempfile
import json
import zipfile
from datetime import timedelta
from io import BytesIO, StringIO
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
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
from .ai import KnowledgeAiError, generate_proposals, knowledge_ai_provider
from .crypto import decrypt_json, encrypt_json
from .microsoft import GraphResponse, MicrosoftAuthorizationError
from .imports import (
    assign_import_batch_person,
    create_uploaded_import_batch,
    parse_wechat_html,
)
from .models import (
    KnowledgeAsset,
    KnowledgeCategory,
    KnowledgeCurationRevision,
    KnowledgeDocument,
    KnowledgeImportBatch,
    KnowledgeImportItem,
    KnowledgeJob,
    KnowledgeProposal,
    KnowledgeProposalRun,
    KnowledgeRevision,
    KnowledgeSearchEntry,
    KnowledgeSource,
    KnowledgeTag,
    KnowledgeVisibility,
    SourceConnection,
)
from .search import index_document, rebuild_family_search
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

    def test_wechat_parser_extracts_article_body_and_metadata_only(self):
        raw = b"""<!doctype html><html><head>
        <meta name="author" content="Test Author">
        <meta property="og:title" content="Test Title">
        <meta property="og:url" content="https://mp.weixin.qq.com/s?__biz=biz&amp;mid=1&amp;idx=1">
        </head><body><h1 id="activity-name">Fallback</h1>
        <em id="publish_time">2026-08-08 09:30</em>
        <div id="js_content"><p>Article body</p><script>bad()</script></div>
        <div class="comment-item">Comment body</div></body></html>"""

        article = parse_wechat_html(raw, "[202608080930]fallback.html")

        self.assertEqual(article.title, "Test Title")
        self.assertEqual(article.author, "Test Author")
        self.assertEqual(article.published_at.strftime("%Y-%m-%d %H:%M"), "2026-08-08 09:30")
        self.assertIn("Article body", article.body_template)
        self.assertNotIn("bad()", article.body_template)
        self.assertNotIn("Comment body", article.body_template)

    def test_wechat_parser_recovers_text_only_post_from_metadata(self):
        raw = b"""<!doctype html><html><head>
        <meta property="og:title" content="First paragraph.\\n\\nSecond paragraph.">
        <meta property="og:url" content="https://mp.weixin.qq.com/s?__biz=biz&amp;mid=2&amp;idx=1">
        </head><body></body></html>"""

        article = parse_wechat_html(raw, "[202608080930]long-filename.html")

        self.assertEqual(article.title, "First paragraph.")
        self.assertIn("First paragraph.", article.body_template)
        self.assertIn("Second paragraph.", article.body_template)
        self.assertIn("纯文字动态", article.warnings[1])


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
            library_tier=KnowledgeDocument.LIBRARY_KNOWLEDGE,
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

    def make_pending_document(self, **kwargs):
        document = self.make_document(**kwargs)
        document.knowledge_status = KnowledgeDocument.KNOWLEDGE_PENDING
        document.library_tier = KnowledgeDocument.LIBRARY_ARCHIVE
        document.curation_status = KnowledgeDocument.CURATION_NORMALIZED
        document.save(
            update_fields=[
                "knowledge_status",
                "library_tier",
                "curation_status",
                "updated_at",
            ]
        )
        index_document(document)
        return document

    def make_text_ai_provider(self):
        return AiProvider.objects.create(
            name="文本测试服务",
            provider_type="openai_compatible",
            base_url="https://api.example.com/v1",
            model_name="test-text-model",
            extra_data={"api_key_env_var": "TEST_KNOWLEDGE_AI_KEY"},
        )

    def make_html_import_batch(
        self,
        *,
        member=None,
        visibility=KnowledgeVisibility.FAMILY,
        title="测试文章",
        timestamp="202608080930",
        source_name="微信公众号 · 测试号",
        person_name="",
        mid="100",
        body="正文内容",
    ):
        member = member or self.member
        stem = f"[{timestamp}]{title}"
        html_body = f"""<!doctype html><html><head>
        <meta name="author" content="测试作者">
        <meta property="og:title" content="{title}">
        <meta property="og:url" content="https://mp.weixin.qq.com/s?__biz=testbiz&amp;mid={mid}&amp;idx=1">
        </head><body><img src="https://example.com/cover.jpg">
        <em id="publish_time">2026-08-08 09:30</em>
        <div id="js_content"><p>{body}</p><script>bad()</script>
        <img data-src="https://example.com/article.jpg" alt="正文图"></div>
        <div class="comment-item">不应导入的评论</div></body></html>""".encode("utf-8")
        package = BytesIO()
        with zipfile.ZipFile(package, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(f"{stem}.html", html_body)
            archive.writestr(
                f"图片/{title}/{stem}_{1}.jpg",
                b"\xff\xd8\xffarticle-image",
            )
            archive.writestr(
                f"封面/[{timestamp}]_{title}.jpg",
                b"\xff\xd8\xffcover-image",
            )
        uploaded = SimpleUploadedFile(
            "wechat-sample.zip",
            package.getvalue(),
            content_type="application/zip",
        )
        return create_uploaded_import_batch(
            member=member,
            source_name=source_name,
            person_name=person_name,
            category="公众号归档",
            visibility=visibility,
            uploaded_file=uploaded,
        )

    def test_html_import_previews_imports_skips_repeat_and_rolls_back(self):
        batch = self.make_html_import_batch()
        preview_job, _ = queue_knowledge_job(
            family=self.family,
            source=batch.source,
            requested_by=self.member,
            job_type=KnowledgeJob.TYPE_PREVIEW_IMPORT,
            parameters={"batch_id": batch.pk},
        )

        process_job(preview_job)

        preview_job.refresh_from_db()
        batch.refresh_from_db()
        self.assertEqual(preview_job.status, KnowledgeJob.STATUS_SUCCESS)
        self.assertEqual(batch.status, KnowledgeImportBatch.STATUS_PREVIEW_READY)
        self.assertEqual(batch.new_count, 1)
        self.assertEqual(batch.asset_count, 2)
        self.assertFalse(KnowledgeDocument.objects.exists())

        import_job, _ = queue_knowledge_job(
            family=self.family,
            source=batch.source,
            requested_by=self.member,
            job_type=KnowledgeJob.TYPE_IMPORT_BATCH,
            parameters={"batch_id": batch.pk},
        )
        process_job(import_job)

        import_job.refresh_from_db()
        batch.refresh_from_db()
        item = batch.items.get()
        document = KnowledgeDocument.objects.get()
        revision = document.current_revision
        self.assertEqual(import_job.status, KnowledgeJob.STATUS_SUCCESS)
        self.assertEqual(batch.status, KnowledgeImportBatch.STATUS_COMPLETED)
        self.assertEqual(document.knowledge_status, KnowledgeDocument.KNOWLEDGE_INCLUDED)
        self.assertEqual(document.library_tier, KnowledgeDocument.LIBRARY_ARCHIVE)
        self.assertEqual(document.curation_status, KnowledgeDocument.CURATION_NORMALIZED)
        self.assertEqual(document.author, "测试作者")
        self.assertEqual(document.category, "公众号归档")
        self.assertIn("正文内容", revision.plain_text)
        self.assertNotIn("不应导入的评论", revision.plain_text)
        self.assertNotIn("bad()", revision.normalized_html)
        self.assertIn("/knowledge/assets/", revision.normalized_html)
        self.assertEqual(revision.assets.count(), 2)
        self.assertTrue(revision.raw_file.storage.exists(revision.raw_file.name))
        self.assertEqual(item.status, KnowledgeImportItem.STATUS_IMPORTED)

        repeat = self.make_html_import_batch()
        repeat_preview, _ = queue_knowledge_job(
            family=self.family,
            source=repeat.source,
            requested_by=self.member,
            job_type=KnowledgeJob.TYPE_PREVIEW_IMPORT,
            parameters={"batch_id": repeat.pk},
        )
        process_job(repeat_preview)
        repeat.refresh_from_db()
        self.assertEqual(repeat.skipped_count, 1)
        self.assertEqual(repeat.items.get().action, KnowledgeImportItem.ACTION_UNCHANGED)
        self.assertEqual(KnowledgeDocument.objects.count(), 1)
        self.assertEqual(KnowledgeRevision.objects.count(), 1)

        rollback_job, _ = queue_knowledge_job(
            family=self.family,
            source=batch.source,
            requested_by=self.member,
            job_type=KnowledgeJob.TYPE_ROLLBACK_IMPORT,
            parameters={"batch_id": batch.pk},
        )
        process_job(rollback_job)
        rollback_job.refresh_from_db()
        batch.refresh_from_db()
        self.assertEqual(rollback_job.status, KnowledgeJob.STATUS_SUCCESS)
        self.assertEqual(batch.status, KnowledgeImportBatch.STATUS_ROLLED_BACK)
        self.assertFalse(KnowledgeDocument.objects.exists())
        self.assertTrue(batch.package_file.storage.exists(batch.package_file.name))

    def test_html_import_can_unify_source_bylines_under_one_person(self):
        batch = self.make_html_import_batch(person_name="金渐成", mid="125")
        preview_job, _ = queue_knowledge_job(
            family=self.family,
            source=batch.source,
            requested_by=self.member,
            job_type=KnowledgeJob.TYPE_PREVIEW_IMPORT,
            parameters={"batch_id": batch.pk},
        )
        process_job(preview_job)

        item = batch.items.get()
        self.assertEqual(item.author, "金渐成")
        self.assertEqual(item.details["source_author"], "测试作者")

        import_job, _ = queue_knowledge_job(
            family=self.family,
            source=batch.source,
            requested_by=self.member,
            job_type=KnowledgeJob.TYPE_IMPORT_BATCH,
            parameters={"batch_id": batch.pk},
        )
        process_job(import_job)

        document = KnowledgeDocument.objects.get()
        self.assertEqual(document.author, "金渐成")
        self.assertEqual(document.import_items.get().details["source_author"], "测试作者")

    def test_html_import_reuses_person_for_the_same_source(self):
        first = self.make_html_import_batch(person_name="金渐成", mid="130")
        second = self.make_html_import_batch(person_name="", mid="131")

        self.assertEqual(first.source_id, second.source_id)
        self.assertEqual(second.person_name, "金渐成")

    def test_completed_import_can_assign_person_without_breaking_safe_rollback(self):
        batch = self.make_html_import_batch(mid="135")
        preview_job, _ = queue_knowledge_job(
            family=self.family,
            source=batch.source,
            requested_by=self.member,
            job_type=KnowledgeJob.TYPE_PREVIEW_IMPORT,
            parameters={"batch_id": batch.pk},
        )
        process_job(preview_job)
        import_job, _ = queue_knowledge_job(
            family=self.family,
            source=batch.source,
            requested_by=self.member,
            job_type=KnowledgeJob.TYPE_IMPORT_BATCH,
            parameters={"batch_id": batch.pk},
        )
        process_job(import_job)

        result = assign_import_batch_person(batch, "金渐成")
        batch.refresh_from_db()
        item = batch.items.get()
        document = KnowledgeDocument.objects.get()
        self.assertEqual(result["source_aliases"], ["测试作者"])
        self.assertEqual(batch.person_name, "金渐成")
        self.assertEqual(item.author, "金渐成")
        self.assertEqual(item.details["source_author"], "测试作者")
        self.assertEqual(document.author, "金渐成")
        self.assertEqual(
            item.details["document_updated_at"],
            document.updated_at.isoformat(),
        )

        rollback_job, _ = queue_knowledge_job(
            family=self.family,
            source=batch.source,
            requested_by=self.member,
            job_type=KnowledgeJob.TYPE_ROLLBACK_IMPORT,
            parameters={"batch_id": batch.pk},
        )
        process_job(rollback_job)
        self.assertFalse(KnowledgeDocument.objects.exists())

    def test_html_import_rolls_back_database_when_search_indexing_fails(self):
        batch = self.make_html_import_batch(title="索引失败样本", mid="150")
        preview_job, _ = queue_knowledge_job(
            family=self.family,
            source=batch.source,
            requested_by=self.member,
            job_type=KnowledgeJob.TYPE_PREVIEW_IMPORT,
            parameters={"batch_id": batch.pk},
        )
        process_job(preview_job)
        import_job, _ = queue_knowledge_job(
            family=self.family,
            source=batch.source,
            requested_by=self.member,
            job_type=KnowledgeJob.TYPE_IMPORT_BATCH,
            parameters={"batch_id": batch.pk},
        )

        with patch("knowledge.imports.index_document", side_effect=RuntimeError("索引失败")):
            process_job(import_job)

        import_job.refresh_from_db()
        batch.refresh_from_db()
        self.assertEqual(import_job.status, KnowledgeJob.STATUS_PARTIAL)
        self.assertEqual(batch.status, KnowledgeImportBatch.STATUS_PARTIAL)
        self.assertEqual(batch.items.get().status, KnowledgeImportItem.STATUS_FAILED)
        self.assertFalse(KnowledgeDocument.objects.exists())
        self.assertFalse(KnowledgeRevision.objects.exists())
        self.assertFalse(KnowledgeAsset.objects.exists())
        self.assertTrue(batch.package_file.storage.exists(batch.package_file.name))

        retry_job, _ = queue_knowledge_job(
            family=self.family,
            source=batch.source,
            requested_by=self.member,
            job_type=KnowledgeJob.TYPE_IMPORT_BATCH,
            parameters={"batch_id": batch.pk},
        )
        process_job(retry_job)
        retry_job.refresh_from_db()
        batch.refresh_from_db()
        self.assertEqual(retry_job.status, KnowledgeJob.STATUS_SUCCESS)
        self.assertEqual(batch.status, KnowledgeImportBatch.STATUS_COMPLETED)
        self.assertEqual(KnowledgeDocument.objects.count(), 1)
        self.assertEqual(KnowledgeRevision.objects.count(), 1)

    def test_html_import_rollback_refuses_to_overwrite_later_member_change(self):
        batch = self.make_html_import_batch(title="回滚保护样本", mid="175")
        preview_job, _ = queue_knowledge_job(
            family=self.family,
            source=batch.source,
            requested_by=self.member,
            job_type=KnowledgeJob.TYPE_PREVIEW_IMPORT,
            parameters={"batch_id": batch.pk},
        )
        process_job(preview_job)
        import_job, _ = queue_knowledge_job(
            family=self.family,
            source=batch.source,
            requested_by=self.member,
            job_type=KnowledgeJob.TYPE_IMPORT_BATCH,
            parameters={"batch_id": batch.pk},
        )
        process_job(import_job)
        document = KnowledgeDocument.objects.get()
        document.title = "成员后来修改的标题"
        document.save(update_fields=["title", "updated_at"])
        rollback_job, _ = queue_knowledge_job(
            family=self.family,
            source=batch.source,
            requested_by=self.member,
            job_type=KnowledgeJob.TYPE_ROLLBACK_IMPORT,
            parameters={"batch_id": batch.pk},
        )

        process_job(rollback_job)

        rollback_job.refresh_from_db()
        batch.refresh_from_db()
        document.refresh_from_db()
        self.assertEqual(rollback_job.status, KnowledgeJob.STATUS_PARTIAL)
        self.assertEqual(batch.status, KnowledgeImportBatch.STATUS_PARTIAL)
        self.assertEqual(document.title, "成员后来修改的标题")
        self.assertIn("已有修改", batch.items.get().error_message)
        detail_response = self.client.get(
            reverse("knowledge:import_batch_detail", kwargs={"pk": batch.pk})
        )
        self.assertContains(detail_response, "安全回滚这个批次")
        self.assertNotContains(detail_response, "重试失败项目")

    def test_html_import_web_upload_requires_confirmation_before_documents_exist(self):
        package_batch = self.make_html_import_batch(title="网页上传样本", mid="200")
        with package_batch.package_file.open("rb") as package:
            uploaded = SimpleUploadedFile(
                "browser-upload.zip",
                package.read(),
                content_type="application/zip",
            )
        package_batch.delete()

        response = self.client.post(
            reverse("knowledge:imports"),
            {
                "source_name": "微信公众号 · 网页测试",
                "category": "公众号归档",
                "visibility": KnowledgeVisibility.FAMILY,
                "package": uploaded,
            },
        )

        self.assertEqual(response.status_code, 302)
        batch = KnowledgeImportBatch.objects.get()
        self.assertEqual(batch.status, KnowledgeImportBatch.STATUS_UPLOADED)
        self.assertTrue(
            KnowledgeJob.objects.filter(
                job_type=KnowledgeJob.TYPE_PREVIEW_IMPORT,
                parameters__batch_id=batch.pk,
            ).exists()
        )
        self.assertFalse(KnowledgeDocument.objects.exists())

    def test_private_import_items_are_hidden_from_other_members_and_admin(self):
        batch = self.make_html_import_batch(
            member=self.other_member,
            visibility=KnowledgeVisibility.PRIVATE,
            source_name="微信公众号 · 私密测试",
            mid="300",
        )
        preview_job, _ = queue_knowledge_job(
            family=self.family,
            source=batch.source,
            requested_by=self.other_member,
            job_type=KnowledgeJob.TYPE_PREVIEW_IMPORT,
            parameters={"batch_id": batch.pk},
        )
        process_job(preview_job)

        admin_response = self.client.get(
            reverse("knowledge:import_batch_detail", kwargs={"pk": batch.pk})
        )
        self.assertEqual(admin_response.status_code, 200)
        self.assertContains(admin_response, "该批次属于其他成员的私密资料；这里只显示汇总")
        self.assertNotContains(admin_response, "测试文章")

        self.client.force_login(self.other_user)
        owner_response = self.client.get(
            reverse("knowledge:import_batch_detail", kwargs={"pk": batch.pk})
        )
        self.assertEqual(owner_response.status_code, 200)
        self.assertContains(owner_response, "测试文章")

    def test_viewer_cannot_upload_import_package(self):
        batch = self.make_html_import_batch(title="只读成员上传", mid="400")
        with batch.package_file.open("rb") as package:
            uploaded = SimpleUploadedFile(
                "viewer-upload.zip",
                package.read(),
                content_type="application/zip",
            )
        batch.delete()
        self.client.force_login(self.viewer_user)

        response = self.client.post(
            reverse("knowledge:imports"),
            {
                "source_name": "微信公众号 · 只读测试",
                "category": "公众号归档",
                "visibility": KnowledgeVisibility.FAMILY,
                "package": uploaded,
            },
        )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(KnowledgeImportBatch.objects.exists())

    def test_existing_note_projection_updates_without_creating_editable_copy(self):
        own_note = InvestmentNote.objects.create(
            family=self.family,
            member=self.member,
            title="自己的私密灵感",
            content="关于风险控制的想法",
            note_type=self.note_type,
            visibility=InvestmentNote.VISIBILITY_PRIVATE,
            tags=["风险控制"],
            include_in_knowledge=True,
        )
        shared_note = InvestmentNote.objects.create(
            family=self.family,
            member=self.other_member,
            title="家庭共享灵感",
            content="家庭都可以看到",
            note_type=self.note_type,
            visibility=InvestmentNote.VISIBILITY_FAMILY,
            include_in_knowledge=True,
        )
        hidden_note = InvestmentNote.objects.create(
            family=self.family,
            member=self.other_member,
            title="其他成员私密灵感",
            content="不可泄露",
            note_type=self.note_type,
            visibility=InvestmentNote.VISIBILITY_PRIVATE,
            include_in_knowledge=True,
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
        pending_document = self.make_document(
            source=document.source,
            title="等待提炼的摘录",
            external_id="pending-hub-page",
        )
        pending_document.knowledge_status = KnowledgeDocument.KNOWLEDGE_PENDING
        pending_document.save(update_fields=["knowledge_status", "updated_at"])
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
        self.assertContains(home, "待整理")
        self.assertContains(home, "在线阅读")
        self.assertContains(home, "交易复盘")
        self.assertContains(home, document.title)
        self.assertContains(home, "knowledge-help-tip-icon")

        inbox = self.client.get(reverse("knowledge:inbox"))
        self.assertContains(inbox, pending_document.title)
        self.assertContains(inbox, "知识目录")
        self.assertContains(inbox, "精选知识")
        self.assertContains(inbox, "尚未整理")
        self.assertContains(inbox, "等待确认")
        self.assertNotContains(inbox, "收件箱")
        self.assertNotContains(inbox, "已规范化")
        self.assertNotContains(inbox, "AI 建议确认")
        self.assertNotContains(inbox, document.title)

        library = self.client.get(
            reverse("knowledge:library"),
            {"q": "半导体"},
        )
        self.assertContains(library, document.title)

        topics = self.client.get(reverse("knowledge:topics"))
        self.assertContains(topics, "#半导体")
        self.assertContains(topics, "投资研究")
        self.assertContains(topics, "knowledge-help-tip-icon")

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
        self.assertContains(person_timeline, "未关联人物档案")
        self.assertContains(person_timeline, "去核验")
        self.assertContains(person_timeline, "搜索这个人物的文章")

    def test_people_page_searches_filters_and_paginates_all_history(self):
        source = self.make_source(suffix="person-history")
        for index in range(22):
            document = self.make_document(
                source=source,
                title=("目标关键词文章" if index == 21 else f"历史文章 {index:02d}"),
                external_id=f"person-history-{index}",
                plain_text=f"人物历史正文 {index}",
            )
            document.author = "分页作者"
            update_fields = ["author", "updated_at"]
            if index == 0:
                document.library_tier = KnowledgeDocument.LIBRARY_ARCHIVE
                update_fields.append("library_tier")
            elif index == 1:
                document.knowledge_status = KnowledgeDocument.KNOWLEDGE_PENDING
                document.library_tier = KnowledgeDocument.LIBRARY_ARCHIVE
                update_fields.extend(["knowledge_status", "library_tier"])
            document.save(update_fields=update_fields)
            index_document(document)

        first_page = self.client.get(
            reverse("knowledge:people"),
            {"person": "分页作者"},
        )
        self.assertEqual(first_page.context["timeline_page"].paginator.count, 22)
        self.assertEqual(len(first_page.context["timeline_page"]), 20)
        self.assertContains(first_page, "第 1 / 2 页")
        self.assertContains(first_page, "全部 <strong>22</strong>", html=True)

        second_page = self.client.get(
            reverse("knowledge:people"),
            {"person": "分页作者", "page": 2},
        )
        self.assertEqual(len(second_page.context["timeline_page"]), 2)

        search = self.client.get(
            reverse("knowledge:people"),
            {"person": "分页作者", "q": "目标关键词"},
        )
        self.assertEqual(search.context["timeline_page"].paginator.count, 1)
        self.assertContains(search, "目标关键词文章")
        self.assertContains(search, "“目标关键词”找到 1 篇")

        archived = self.client.get(
            reverse("knowledge:people"),
            {"person": "分页作者", "status": "archive"},
        )
        self.assertEqual(archived.context["timeline_page"].paginator.count, 1)

        curated = self.client.get(
            reverse("knowledge:people"),
            {"person": "分页作者", "status": "curated"},
        )
        self.assertEqual(curated.context["timeline_page"].paginator.count, 20)

        pending = self.client.get(
            reverse("knowledge:people"),
            {"person": "分页作者", "status": "pending"},
        )
        self.assertEqual(pending.context["timeline_page"].paginator.count, 1)

        self.client.force_login(self.other_user)
        member_view = self.client.get(
            reverse("knowledge:people"),
            {"person": "分页作者"},
        )
        self.assertContains(member_view, "未关联人物档案")
        self.assertNotContains(member_view, "去核验")

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
        self.assertEqual(default_library.context["selected_collection"], "curated")
        self.assertContains(default_library, private_document.title)
        self.assertContains(default_library, family_document.title)
        self.assertNotContains(default_library, other_family_document.title)
        self.assertContains(default_library, "默认显示当前成员的精选知识")
        self.assertContains(default_library, "资料分区")

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

    def test_imported_articles_use_archive_views_until_manually_promoted(self):
        source = self.make_source(suffix="wechat-history")
        source.kind = KnowledgeSource.KIND_HTML_IMPORT
        source.name = "微信公众号 · 测试账号"
        source.save(update_fields=["kind", "name", "updated_at"])
        document = self.make_document(
            source=source,
            title="公众号历史文章",
            external_id="wechat-history-page",
        )
        document.library_tier = KnowledgeDocument.LIBRARY_ARCHIVE
        document.author = "测试作者"
        document.save(update_fields=["library_tier", "author", "updated_at"])
        index_document(document)

        library = self.client.get(reverse("knowledge:library"))
        self.assertEqual(library.context["selected_collection"], "curated")
        self.assertNotContains(
            library,
            f'href="/knowledge/documents/{document.pk}/"',
        )
        formal_library = self.client.get(
            reverse("knowledge:library"), {"collection": "archive"}
        )
        self.assertContains(formal_library, document.title)
        self.assertNotContains(formal_library, "查看原文")
        curated = self.client.get(
            reverse("knowledge:library"), {"collection": "curated"}
        )
        self.assertNotContains(
            curated,
            f'href="/knowledge/documents/{document.pk}/"',
        )
        archive = self.client.get(
            reverse("knowledge:library"), {"collection": "archive"}
        )
        self.assertContains(archive, document.title)
        source_directory = self.client.get(
            reverse("knowledge:library"),
            {"collection": "archive", "directory": "source"},
        )
        self.assertContains(source_directory, "关注人物")
        self.assertContains(source_directory, document.author)

        people = self.client.get(reverse("knowledge:people"))
        self.assertContains(people, document.author)
        self.assertContains(people, "历史文章")
        history = self.client.get(
            reverse("knowledge:source_history", kwargs={"pk": source.pk})
        )
        self.assertContains(history, document.title)
        self.assertContains(history, "归档资料")

        queue = self.client.post(
            reverse("knowledge:document_add_to_inbox", kwargs={"pk": document.pk})
        )
        self.assertRedirects(
            queue,
            reverse("knowledge:document_detail", kwargs={"pk": document.pk}),
        )
        document.refresh_from_db()
        self.assertEqual(
            document.knowledge_status,
            KnowledgeDocument.KNOWLEDGE_PENDING,
        )
        self.assertContains(self.client.get(reverse("knowledge:inbox")), document.title)

        cancel = self.client.post(
            reverse(
                "knowledge:document_cancel_organizing",
                kwargs={"pk": document.pk},
            )
        )
        self.assertRedirects(
            cancel,
            reverse("knowledge:document_detail", kwargs={"pk": document.pk}),
        )
        document.refresh_from_db()
        self.assertEqual(
            document.knowledge_status,
            KnowledgeDocument.KNOWLEDGE_INCLUDED,
        )
        self.client.post(
            reverse("knowledge:document_add_to_inbox", kwargs={"pk": document.pk})
        )

        organize = self.client.post(
            reverse("knowledge:document_organize", kwargs={"pk": document.pk}),
            {
                "confirmed_summary": "值得长期复用的摘要",
                "category": "投资经验",
                "tags_text": "长期复用",
                "visibility": KnowledgeVisibility.FAMILY,
            },
        )
        self.assertRedirects(
            organize,
            reverse("knowledge:document_detail", kwargs={"pk": document.pk}),
        )
        document.refresh_from_db()
        self.assertTrue(
            KnowledgeCategory.objects.filter(
                family=self.family,
                name="投资经验",
            ).exists()
        )
        self.assertTrue(
            KnowledgeTag.objects.filter(
                family=self.family,
                name="长期复用",
            ).exists()
        )
        curation_revision = document.curation_revisions.get()
        self.assertEqual(
            curation_revision.change_type,
            KnowledgeCurationRevision.TYPE_MANUAL,
        )
        curated = self.client.get(
            reverse("knowledge:library"), {"collection": "curated"}
        )
        self.assertContains(curated, document.title)
        self.assertContains(
            self.client.get(
                reverse("knowledge:source_history", kwargs={"pk": source.pk})
            ),
            document.title,
        )

    def test_document_detail_reading_controls_and_import_spacing_class(self):
        source = self.make_source(suffix="wechat-reading-controls")
        source.kind = KnowledgeSource.KIND_HTML_IMPORT
        source.save(update_fields=["kind", "updated_at"])
        document = self.make_document(
            source=source,
            title="阅读显示设置",
            external_id="wechat-reading-controls-page",
            normalized_html="<p>第一段</p><p><br></p><p>第二段</p>",
        )

        response = self.client.get(
            reverse("knowledge:document_detail", kwargs={"pk": document.pk}),
            {"font_size": "large", "line_spacing": "compact"},
        )

        self.assertEqual(response.context["reading_font_size"], "large")
        self.assertEqual(response.context["reading_line_spacing"], "compact")
        self.assertContains(
            response,
            "knowledge-rich-content reading-size-large reading-spacing-compact knowledge-rich-content-imported",
        )
        self.assertContains(response, "字号")
        self.assertContains(response, "行间距")
        self.assertContains(response, 'name="font_size" value="small"')
        self.assertContains(response, 'name="line_spacing" value="compact"')

        saved = self.client.post(
            reverse(
                "knowledge:document_reading_preferences",
                kwargs={"pk": document.pk},
            ),
            {"font_size": "large", "line_spacing": "compact"},
        )
        self.assertRedirects(
            saved,
            reverse("knowledge:document_detail", kwargs={"pk": document.pk}),
        )
        self.member.refresh_from_db()
        self.assertEqual(
            self.member.extra_data["knowledge_reading"],
            {"font_size": "large", "line_spacing": "compact"},
        )
        self.client.logout()
        self.client.force_login(self.user)
        persisted = self.client.get(
            reverse("knowledge:document_detail", kwargs={"pk": document.pk})
        )
        self.assertContains(
            persisted,
            "knowledge-rich-content reading-size-large reading-spacing-compact knowledge-rich-content-imported",
        )

        fallback = self.client.get(
            reverse("knowledge:document_detail", kwargs={"pk": document.pk}),
            {"font_size": "unknown", "line_spacing": "unknown"},
        )
        self.assertContains(
            fallback,
            "knowledge-rich-content reading-size-medium reading-spacing-normal knowledge-rich-content-imported",
        )

    def test_library_display_mode_uses_member_cookie(self):
        cookie_name = f"knowledge_library_display_mode_{self.member.pk}"

        self.client.cookies[cookie_name] = "compact"
        compact = self.client.get(reverse("knowledge:library"))
        self.assertEqual(compact.context["display_mode"], "compact")
        self.assertContains(compact, "knowledge-compact-table")
        self.assertNotContains(compact, "knowledge-result-list-cards")

        self.client.cookies[cookie_name] = "cards"
        cards = self.client.get(reverse("knowledge:library"))
        self.assertEqual(cards.context["display_mode"], "cards")
        self.assertContains(cards, "knowledge-result-list-cards")

        self.client.cookies[cookie_name] = "unsupported"
        fallback = self.client.get(reverse("knowledge:library"))
        self.assertEqual(fallback.context["display_mode"], "standard")

    def test_compact_pending_view_uses_a_separate_selection_rail(self):
        self.make_pending_document(
            external_id="compact-selection-page",
            title="紧凑视图选择栏",
        )
        cookie_name = f"knowledge_library_display_mode_{self.member.pk}"
        self.client.cookies[cookie_name] = "compact"

        response = self.client.get(reverse("knowledge:inbox"))

        self.assertContains(response, "knowledge-compact-row-layout")
        self.assertContains(response, "knowledge-ai-select knowledge-compact-select")
        html = response.content.decode("utf-8")
        select_position = html.index("knowledge-ai-select knowledge-compact-select")
        meta_position = html.index("knowledge-compact-meta", select_position)
        self.assertLess(select_position, meta_position)

    def test_taxonomy_management_renames_merges_and_protects_used_items(self):
        created = self.client.post(
            reverse("knowledge:taxonomy_create", kwargs={"kind": "category"}),
            {
                "name": "人生经验",
                "description": "长期生活经验",
                "is_active": "on",
            },
        )
        self.assertRedirects(created, reverse("knowledge:topics"))
        self.assertTrue(
            KnowledgeCategory.objects.filter(
                family=self.family,
                name="人生经验",
            ).exists()
        )
        category = KnowledgeCategory.objects.create(
            family=self.family,
            name="投资经验",
            created_by=self.member,
        )
        source_tag = KnowledgeTag.objects.create(
            family=self.family,
            name="长期投资",
            created_by=self.member,
        )
        target_tag = KnowledgeTag.objects.create(
            family=self.family,
            name="长期主义",
            created_by=self.member,
        )
        document = self.make_document(
            external_id="taxonomy-page",
            title="分类标签治理",
        )
        document.category = category.name
        document.tags = [source_tag.name]
        document.save(update_fields=["category", "tags", "updated_at"])
        index_document(document)

        renamed = self.client.post(
            reverse(
                "knowledge:taxonomy_edit",
                kwargs={"kind": "category", "pk": category.pk},
            ),
            {
                "name": "投资方法",
                "description": "可复用的投资方法",
                "is_active": "on",
            },
        )
        self.assertRedirects(renamed, reverse("knowledge:topics"))
        category.refresh_from_db()
        document.refresh_from_db()
        self.assertEqual(category.name, "投资方法")
        self.assertIn("投资经验", category.aliases)
        self.assertEqual(document.category, "投资方法")
        self.assertEqual(document.search_entry.category, "投资方法")

        merged = self.client.post(
            reverse(
                "knowledge:taxonomy_merge",
                kwargs={"kind": "tag", "pk": source_tag.pk},
            ),
            {"target": target_tag.pk},
        )
        self.assertRedirects(merged, reverse("knowledge:topics"))
        source_tag.refresh_from_db()
        document.refresh_from_db()
        self.assertEqual(source_tag.merged_into, target_tag)
        self.assertEqual(document.tags, ["长期主义"])

        protected = self.client.post(
            reverse(
                "knowledge:taxonomy_delete",
                kwargs={"kind": "tag", "pk": target_tag.pk},
            )
        )
        self.assertRedirects(protected, reverse("knowledge:topics"))
        self.assertTrue(KnowledgeTag.objects.filter(pk=target_tag.pk).exists())

        self.client.force_login(self.other_user)
        denied = self.client.post(
            reverse("knowledge:taxonomy_create", kwargs={"kind": "tag"}),
            {"name": "越权标签", "is_active": "on"},
        )
        self.assertEqual(denied.status_code, 403)

    def test_library_directory_filters_by_category_and_onenote_source_path(self):
        source = self.make_source(suffix="reading")
        book_list = self.make_document(
            source=source,
            title="年度阅读书单",
            external_id="book-list-page",
        )
        book_list.category = "书单"
        book_list.section_name = "书单"
        book_list.hierarchy = {
            "notebook_name": source.name,
            "section_group": "读书心得",
        }
        book_list.save(
            update_fields=["category", "section_name", "hierarchy", "updated_at"]
        )
        index_document(book_list)

        life_note = self.make_document(
            source=source,
            title="长期习惯记录",
            external_id="life-page",
        )
        life_note.category = "人生经验"
        life_note.section_name = "人生经验"
        life_note.hierarchy = {
            "notebook_name": source.name,
            "section_group": "读书心得",
        }
        life_note.save(
            update_fields=["category", "section_name", "hierarchy", "updated_at"]
        )
        index_document(life_note)

        response = self.client.get(reverse("knowledge:library"))
        self.assertContains(response, "知识目录")
        self.assertContains(response, "按分类")
        self.assertContains(response, "按来源")
        self.assertContains(response, "分类：书单")
        self.assertContains(
            response,
            f"原始位置：{source.name} / 读书心得 / 书单",
        )

        category_response = self.client.get(
            reverse("knowledge:library"),
            {"category": "书单", "directory": "category"},
        )
        self.assertContains(category_response, book_list.title)
        self.assertNotContains(category_response, life_note.title)

        source_response = self.client.get(
            reverse("knowledge:library"),
            {
                "directory": "source",
                "source_id": source.pk,
                "section_group": "读书心得",
                "section": "人生经验",
            },
        )
        self.assertContains(source_response, life_note.title)
        self.assertNotContains(source_response, book_list.title)
        self.assertContains(source_response, "读书心得 / 人生经验")

        source_directory = self.client.get(
            reverse("knowledge:library"),
            {"directory": "source"},
        )
        self.assertContains(source_directory, "OneNote")
        self.assertContains(source_directory, source.name)

        detail = self.client.get(
            reverse("knowledge:document_detail", kwargs={"pk": book_list.pk})
        )
        self.assertContains(detail, "原始位置")
        self.assertContains(detail, f"{source.name} / 读书心得 / 书单")

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
        self.assertEqual(source.default_route, KnowledgeSource.ROUTE_KNOWLEDGE)
        self.assertFalse(KnowledgeJob.objects.exists())

    def test_source_detail_only_manages_ai_consent_and_links_to_pending(self):
        source = self.make_source(suffix="source-boundary")
        document = self.make_pending_document(
            source=source,
            title="来源下的待整理文章",
            external_id="source-boundary-page",
        )

        response = self.client.get(
            reverse("knowledge:source_detail", kwargs={"pk": source.pk})
        )

        self.assertContains(response, "云端 AI 正文发送")
        self.assertContains(response, "每次使用前确认")
        self.assertContains(response, "今后允许将本来源正文发送给云端 AI 整理")
        self.assertContains(response, "查看待整理（1）")
        self.assertContains(response, f"source_id={source.pk}")
        self.assertNotContains(response, "为待整理资料创建 AI 任务")
        self.assertNotContains(response, "generate-proposals")
        self.assertEqual(document.knowledge_status, KnowledgeDocument.KNOWLEDGE_PENDING)

    def test_source_list_keeps_history_link_outside_source_detail_link(self):
        source = self.make_source(suffix="source-list-links")
        source.kind = KnowledgeSource.KIND_HTML_IMPORT
        source.name = "公众号历史资料 · 测试账号"
        source.key = "html:source-list-links"
        source.save(update_fields=["kind", "name", "key", "updated_at"])

        response = self.client.get(reverse("knowledge:sources"))

        self.assertContains(response, f'href="/knowledge/sources/{source.pk}/"')
        self.assertContains(
            response,
            f'href="/knowledge/sources/{source.pk}/history/"',
        )
        self.assertNotContains(response, '<a class="source-row"')

    def test_onenote_section_routes_can_reclassify_existing_documents(self):
        source = self.make_source(suffix="section-routing")
        direct = self.make_document(
            source=source,
            title="人生经验",
            external_id="direct-page",
        )
        direct.section_name = "人生经验"
        direct.hierarchy = {"section_id": "section-direct"}
        direct.save(update_fields=["section_name", "hierarchy", "updated_at"])
        organize = self.make_document(
            source=source,
            title="临时摘录",
            external_id="organize-page",
        )
        organize.section_name = "快速笔记"
        organize.hierarchy = {"section_id": "section-organize"}
        organize.save(update_fields=["section_name", "hierarchy", "updated_at"])

        response = self.client.post(
            reverse("knowledge:source_update", kwargs={"pk": source.pk}),
            {
                "visibility": KnowledgeVisibility.FAMILY,
                "default_route": KnowledgeSource.ROUTE_KNOWLEDGE,
                "section_id": ["section-direct", "section-organize"],
                "section_route": [
                    KnowledgeSource.ROUTE_KNOWLEDGE,
                    KnowledgeSource.ROUTE_ORGANIZE,
                ],
                "apply_existing": "on",
            },
        )

        self.assertRedirects(
            response,
            reverse("knowledge:source_detail", kwargs={"pk": source.pk}),
        )
        source.refresh_from_db()
        direct.refresh_from_db()
        organize.refresh_from_db()
        self.assertEqual(
            source.route_for_section("section-organize"),
            KnowledgeSource.ROUTE_ORGANIZE,
        )
        self.assertEqual(
            direct.knowledge_status,
            KnowledgeDocument.KNOWLEDGE_INCLUDED,
        )
        self.assertEqual(
            organize.knowledge_status,
            KnowledgeDocument.KNOWLEDGE_PENDING,
        )
        self.assertNotContains(
            self.client.get(reverse("knowledge:library")),
            f'href="/knowledge/documents/{direct.pk}/"',
        )
        self.assertContains(
            self.client.get(
                reverse("knowledge:library"), {"collection": "archive"}
            ),
            direct.title,
        )
        self.assertNotContains(
            self.client.get(reverse("knowledge:library")),
            organize.title,
        )
        self.assertContains(self.client.get(reverse("knowledge:inbox")), organize.title)

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
        source.config = {
            "default_route": KnowledgeSource.ROUTE_KNOWLEDGE,
            "section_routes": {
                "section-1": KnowledgeSource.ROUTE_ORGANIZE,
            },
        }
        source.save(update_fields=["config", "updated_at"])
        image_url = (
            "https://graph.microsoft.com/v1.0/me/onenote/"
            "resources/image-1/$value"
        )

        class FakeGraphClient:
            def __init__(self):
                self.modified = "2026-07-30T01:00:00Z"
                self.page_ids = ["page-1", "page-2"]
                self.page_content_calls = []
                self.resource_calls = []
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
                self.page_content_calls.append(page_id)
                return self.html[page_id].encode("utf-8")

            def resource(self, url):
                self.resource_calls.append(url)
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
        self.assertEqual(
            page_one.knowledge_status,
            KnowledgeDocument.KNOWLEDGE_PENDING,
        )
        self.assertEqual(page_one.category, "")
        self.assertEqual(
            page_one.library_tier,
            KnowledgeDocument.LIBRARY_ARCHIVE,
        )
        self.assertEqual(page_one.section_name, "财经资料")
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
        self.assertNotContains(
            self.client.get(reverse("knowledge:library")),
            f'href="/knowledge/documents/{page_one.pk}/"',
        )
        self.assertContains(
            self.client.get(reverse("knowledge:library"), {"q": "研究"}),
            page_one.title,
        )
        self.assertContains(
            self.client.get(
                reverse("knowledge:library"),
                {"q": "研究", "collection": "all"},
            ),
            page_one.title,
        )
        self.assertContains(
            self.client.get(reverse("knowledge:inbox")),
            page_one.title,
        )

        page_one.category = "人工修改分类"
        page_one.save(update_fields=["category", "updated_at"])

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
        self.assertEqual(page_one.category, "人工修改分类")
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
        self.assertEqual(len(fake.page_content_calls), 6)
        self.assertEqual(len(fake.resource_calls), 1)

        fake.html["page-1"] = fake.html["page-1"].replace("第一版", "第二版")
        changed = run_sync()
        self.assertEqual(changed.updated_count, 1)
        self.assertEqual(changed.skipped_count, 1)
        page_one.refresh_from_db()
        self.assertEqual(page_one.revisions.count(), 2)
        self.assertIn("第二版", page_one.current_revision.plain_text)
        self.assertEqual(len(fake.resource_calls), 2)
        self.assertEqual(
            page_one.content_modified_at.isoformat(),
            "2026-07-30T01:00:00+00:00",
        )

        fake.modified = "2026-07-30T03:00:00Z"
        metadata_only = run_sync()
        self.assertEqual(metadata_only.updated_count, 0)
        self.assertEqual(metadata_only.skipped_count, 2)
        page_one.refresh_from_db()
        self.assertEqual(page_one.revisions.count(), 2)
        self.assertEqual(len(fake.resource_calls), 2)
        self.assertEqual(
            page_one.content_modified_at.isoformat(),
            "2026-07-30T03:00:00+00:00",
        )

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

    def test_single_ai_organization_uses_one_time_consent_and_selected_scope(self):
        source = self.make_source(suffix="single-ai", allow_cloud_ai=False)
        selected = self.make_pending_document(
            source=source,
            title="准备 AI 整理的文章",
            external_id="single-ai-selected",
        )
        untouched = self.make_pending_document(
            source=source,
            title="本次不处理的文章",
            external_id="single-ai-untouched",
        )
        self.make_text_ai_provider()

        inbox = self.client.get(reverse("knowledge:inbox"))
        self.assertContains(inbox, "AI 批量整理所选文章")
        self.assertContains(inbox, f'name="document_ids" value="{selected.pk}"')
        detail = self.client.get(
            reverse("knowledge:document_detail", kwargs={"pk": selected.pk})
        )
        self.assertContains(detail, "AI 整理")
        self.assertContains(detail, reverse("knowledge:document_ai_organize"))

        preview = self.client.post(
            reverse("knowledge:document_ai_organize"),
            {
                "document_ids": [selected.pk],
                "return_document_id": selected.pk,
            },
        )
        self.assertEqual(preview.status_code, 200)
        self.assertContains(preview, "确认 AI 整理范围")
        self.assertContains(preview, "仅允许本次选中的文章")
        self.assertContains(preview, "文本测试服务 · test-text-model")
        self.assertContains(preview, selected.title)
        self.assertNotContains(preview, untouched.title)

        queued = self.client.post(
            reverse("knowledge:document_ai_organize"),
            {
                "document_ids": [selected.pk],
                "return_document_id": selected.pk,
                "confirm": "yes",
                "authorization": "once",
                "acknowledge": "yes",
            },
        )
        self.assertRedirects(
            queued,
            reverse("knowledge:document_detail", kwargs={"pk": selected.pk}),
        )
        source.refresh_from_db()
        selected.refresh_from_db()
        untouched.refresh_from_db()
        job = KnowledgeJob.objects.get(job_type=KnowledgeJob.TYPE_GENERATE_PROPOSALS)
        self.assertFalse(source.allow_cloud_ai)
        self.assertEqual(job.parameters["document_ids"], [selected.pk])
        self.assertEqual(job.parameters["one_time_document_ids"], [selected.pk])
        self.assertEqual(
            selected.curation_status,
            KnowledgeDocument.CURATION_PENDING_AI,
        )
        self.assertEqual(
            untouched.curation_status,
            KnowledgeDocument.CURATION_NORMALIZED,
        )

        def fake_generate(document, *, cloud_ai_consent, requested_by=None):
            self.assertEqual(document.pk, selected.pk)
            self.assertEqual(cloud_ai_consent, "one_time")
            self.assertEqual(requested_by, self.member)
            document.curation_status = KnowledgeDocument.CURATION_PENDING_REVIEW
            document.save(update_fields=["curation_status", "updated_at"])
            return []

        with patch("knowledge.services.generate_proposals", side_effect=fake_generate) as generate:
            process_job(job)

        self.assertEqual(generate.call_count, 1)
        job.refresh_from_db()
        selected.refresh_from_db()
        untouched.refresh_from_db()
        self.assertEqual(job.status, KnowledgeJob.STATUS_SUCCESS)
        self.assertEqual(
            selected.curation_status,
            KnowledgeDocument.CURATION_PENDING_REVIEW,
        )
        self.assertEqual(selected.knowledge_status, KnowledgeDocument.KNOWLEDGE_PENDING)
        self.assertEqual(
            untouched.curation_status,
            KnowledgeDocument.CURATION_NORMALIZED,
        )
        self.assertEqual(
            KnowledgeSearchEntry.objects.get(document=selected).curation_status,
            KnowledgeDocument.CURATION_PENDING_REVIEW,
        )

    def test_persistent_ai_consent_does_not_process_unselected_source_documents(self):
        source = self.make_source(suffix="persistent-ai", allow_cloud_ai=False)
        selected = self.make_pending_document(
            source=source,
            title="本批次选中文章",
            external_id="persistent-ai-selected",
        )
        untouched = self.make_pending_document(
            source=source,
            title="同来源未选文章",
            external_id="persistent-ai-untouched",
        )
        self.make_text_ai_provider()

        response = self.client.post(
            reverse("knowledge:document_ai_organize"),
            {
                "document_ids": [selected.pk],
                "confirm": "yes",
                "authorization": "source",
                "acknowledge": "yes",
            },
        )

        self.assertRedirects(response, reverse("knowledge:inbox"))
        source.refresh_from_db()
        selected.refresh_from_db()
        untouched.refresh_from_db()
        job = KnowledgeJob.objects.get(job_type=KnowledgeJob.TYPE_GENERATE_PROPOSALS)
        self.assertTrue(source.allow_cloud_ai)
        self.assertEqual(job.parameters["document_ids"], [selected.pk])
        self.assertNotIn("one_time_document_ids", job.parameters)
        self.assertEqual(
            selected.curation_status,
            KnowledgeDocument.CURATION_PENDING_AI,
        )
        self.assertEqual(
            untouched.curation_status,
            KnowledgeDocument.CURATION_NORMALIZED,
        )

    def test_admin_cannot_authorize_ai_for_another_members_source(self):
        source = self.make_source(
            member=self.other_member,
            visibility=KnowledgeVisibility.FAMILY,
            suffix="other-ai-consent",
        )
        document = self.make_pending_document(
            source=source,
            owner=self.other_member,
            visibility=KnowledgeVisibility.FAMILY,
            title="其他成员的待整理资料",
            external_id="other-ai-consent-page",
        )
        self.make_text_ai_provider()

        response = self.client.post(
            reverse("knowledge:document_ai_organize"),
            {"document_ids": [document.pk]},
        )

        self.assertRedirects(response, reverse("knowledge:inbox"))
        source.refresh_from_db()
        self.assertFalse(source.allow_cloud_ai)
        self.assertFalse(
            KnowledgeJob.objects.filter(
                job_type=KnowledgeJob.TYPE_GENERATE_PROPOSALS
            ).exists()
        )

    def test_failed_ai_item_returns_to_unorganized_and_can_be_retried(self):
        source = self.make_source(suffix="failed-ai", allow_cloud_ai=True)
        document = self.make_pending_document(
            source=source,
            title="AI 整理失败文章",
            external_id="failed-ai-page",
        )
        document.curation_status = KnowledgeDocument.CURATION_PENDING_AI
        document.save(update_fields=["curation_status", "updated_at"])
        index_document(document)
        job = KnowledgeJob.objects.create(
            family=self.family,
            source=source,
            requested_by=self.member,
            job_type=KnowledgeJob.TYPE_GENERATE_PROPOSALS,
            parameters={"document_ids": [document.pk]},
        )

        with patch(
            "knowledge.services.generate_proposals",
            side_effect=KnowledgeAiError("测试 AI 失败"),
        ):
            process_job(job)

        job.refresh_from_db()
        document.refresh_from_db()
        self.assertEqual(job.status, KnowledgeJob.STATUS_PARTIAL)
        self.assertEqual(job.failed_count, 1)
        self.assertEqual(
            document.curation_status,
            KnowledgeDocument.CURATION_NORMALIZED,
        )
        self.assertEqual(
            KnowledgeSearchEntry.objects.get(document=document).curation_status,
            KnowledgeDocument.CURATION_NORMALIZED,
        )

    def test_unscoped_ai_job_stops_instead_of_processing_a_whole_source(self):
        source = self.make_source(suffix="unscoped-ai", allow_cloud_ai=True)
        document = self.make_pending_document(
            source=source,
            title="不能整来源自动处理",
            external_id="unscoped-ai-page",
        )
        job = KnowledgeJob.objects.create(
            family=self.family,
            source=source,
            requested_by=self.member,
            job_type=KnowledgeJob.TYPE_GENERATE_PROPOSALS,
        )

        with patch("knowledge.services.generate_proposals") as generate:
            process_job(job)

        job.refresh_from_db()
        document.refresh_from_db()
        self.assertEqual(job.status, KnowledgeJob.STATUS_FAILED)
        self.assertIn("明确选择", job.error_message)
        self.assertEqual(generate.call_count, 0)
        self.assertEqual(
            document.curation_status,
            KnowledgeDocument.CURATION_NORMALIZED,
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
        proposal_run = KnowledgeProposalRun.objects.create(
            document=document,
            revision=document.current_revision,
            sequence=1,
            requested_by=self.member,
            model_name="test-model",
            prompt_version="knowledge-organize-v1",
            content_hash=document.current_revision.content_hash,
        )
        for proposal_type, value in suggested.items():
            proposals[proposal_type] = KnowledgeProposal.objects.create(
                document=document,
                revision=document.current_revision,
                run=proposal_run,
                proposal_type=proposal_type,
                suggested_value=value,
                model_name="test-model",
                prompt_version="knowledge-organize-v1",
                content_hash=document.current_revision.content_hash,
            )
        document.curation_status = KnowledgeDocument.CURATION_PENDING_REVIEW
        document.knowledge_status = KnowledgeDocument.KNOWLEDGE_PENDING
        document.library_tier = KnowledgeDocument.LIBRARY_ARCHIVE
        document.save(
            update_fields=[
                "curation_status",
                "knowledge_status",
                "library_tier",
                "updated_at",
            ]
        )
        index_document(document)

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
        self.assertContains(review_page, "等待确认")
        self.assertContains(review_page, document.title)
        self.assertContains(review_page, "原文 · 不可由 AI 修改")
        self.assertContains(review_page, "预览并确认本篇全部建议")
        waiting_filter = self.client.get(
            reverse("knowledge:inbox"),
            {"stage": "waiting_review"},
        )
        self.assertContains(waiting_filter, document.title)
        unorganized_filter = self.client.get(
            reverse("knowledge:inbox"),
            {"stage": "unorganized"},
        )
        self.assertNotContains(unorganized_filter, document.title)
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
        self.assertEqual(document.curation_revisions.count(), 1)
        self.assertEqual(
            document.curation_revisions.get().proposal_run,
            proposal_run,
        )

    def test_curated_document_can_queue_ai_reorganization_without_leaving_curated(self):
        source = self.make_source(suffix="curated-reorganize", allow_cloud_ai=True)
        document = self.make_document(
            source=source,
            external_id="curated-reorganize-page",
            title="精选知识重新整理",
        )
        document.knowledge_status = KnowledgeDocument.KNOWLEDGE_INCLUDED
        document.library_tier = KnowledgeDocument.LIBRARY_KNOWLEDGE
        document.curation_status = KnowledgeDocument.CURATION_CONFIRMED
        document.confirmed_summary = "当前正式摘要"
        document.save(
            update_fields=[
                "knowledge_status",
                "library_tier",
                "curation_status",
                "confirmed_summary",
                "updated_at",
            ]
        )
        index_document(document)
        self.make_text_ai_provider()

        confirmation = self.client.post(
            reverse("knowledge:document_ai_organize"),
            {
                "document_ids": document.pk,
                "return_document_id": document.pk,
                "mode": "curated_reorganization",
            },
        )
        self.assertEqual(confirmation.status_code, 200)
        self.assertContains(confirmation, "当前精选结果保持不变")

        queued = self.client.post(
            reverse("knowledge:document_ai_organize"),
            {
                "document_ids": document.pk,
                "return_document_id": document.pk,
                "mode": "curated_reorganization",
                "confirm": "yes",
                "authorization": "existing",
                "acknowledge": "yes",
            },
        )
        self.assertRedirects(
            queued,
            reverse("knowledge:document_detail", kwargs={"pk": document.pk}),
        )
        document.refresh_from_db()
        job = KnowledgeJob.objects.get(job_type=KnowledgeJob.TYPE_GENERATE_PROPOSALS)
        self.assertEqual(job.parameters["selection_scope"], "curated_reorganization")
        self.assertEqual(document.curation_status, KnowledgeDocument.CURATION_PENDING_AI)
        self.assertEqual(document.knowledge_status, KnowledgeDocument.KNOWLEDGE_INCLUDED)
        self.assertEqual(document.library_tier, KnowledgeDocument.LIBRARY_KNOWLEDGE)
        self.assertEqual(document.confirmed_summary, "当前正式摘要")
        self.assertNotContains(self.client.get(reverse("knowledge:inbox")), document.title)

    def test_rejecting_all_ai_suggestions_keeps_unorganized_document_pending(self):
        document = self.make_pending_document(
            external_id="reject-all-page",
            title="全部拒绝仍待整理",
        )
        proposal_run = KnowledgeProposalRun.objects.create(
            document=document,
            revision=document.current_revision,
            sequence=1,
            requested_by=self.member,
            model_name="test-model",
            prompt_version="knowledge-organize-v2-taxonomy",
            content_hash=document.current_revision.content_hash,
        )
        proposals = [
            KnowledgeProposal.objects.create(
                document=document,
                revision=document.current_revision,
                run=proposal_run,
                proposal_type=proposal_type,
                suggested_value=value,
                model_name="test-model",
                prompt_version="knowledge-organize-v2-taxonomy",
                content_hash=document.current_revision.content_hash,
            )
            for proposal_type, value in [
                (KnowledgeProposal.TYPE_SUMMARY, {"text": "不采用摘要"}),
                (KnowledgeProposal.TYPE_CATEGORY, {"value": "不采用分类"}),
                (KnowledgeProposal.TYPE_TAGS, {"items": ["不采用标签"]}),
            ]
        ]
        document.curation_status = KnowledgeDocument.CURATION_PENDING_REVIEW
        document.save(update_fields=["curation_status", "updated_at"])

        for proposal in proposals:
            response = self.client.post(
                reverse("knowledge:proposal_review", kwargs={"pk": proposal.pk}),
                {"action": "reject", "value": ""},
            )
            self.assertRedirects(
                response,
                reverse("knowledge:document_detail", kwargs={"pk": document.pk}),
            )

        document.refresh_from_db()
        self.assertEqual(document.knowledge_status, KnowledgeDocument.KNOWLEDGE_PENDING)
        self.assertEqual(document.library_tier, KnowledgeDocument.LIBRARY_ARCHIVE)
        self.assertEqual(document.curation_status, KnowledgeDocument.CURATION_NORMALIZED)
        self.assertFalse(document.curation_revisions.exists())

    @patch("knowledge.ai.socket.getaddrinfo")
    @patch("knowledge.ai.urllib.request.urlopen")
    def test_ai_reorganization_preserves_runs_and_marks_new_taxonomy(
        self,
        urlopen,
        getaddrinfo,
    ):
        source = self.make_source(suffix="ai-runs", allow_cloud_ai=True)
        document = self.make_document(
            source=source,
            external_id="ai-runs-page",
            title="多轮 AI 整理",
        )
        category = KnowledgeCategory.objects.create(
            family=self.family,
            name="投资纪律",
            created_by=self.member,
        )
        tag = KnowledgeTag.objects.create(
            family=self.family,
            name="长期主义",
            created_by=self.member,
        )
        provider = self.make_text_ai_provider()
        getaddrinfo.return_value = [(2, 1, 6, "", ("1.1.1.1", 443))]

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
                                            "summary": "新的整理摘要。",
                                            "category": category.name,
                                            "new_category": "",
                                            "tags": [tag.name],
                                            "new_tags": ["现金流"],
                                        },
                                        ensure_ascii=False,
                                    )
                                }
                            }
                        ],
                        "usage": {"total_tokens": 88},
                    },
                    ensure_ascii=False,
                ).encode("utf-8")

        urlopen.side_effect = [FakeResponse(), FakeResponse()]
        with patch.dict("os.environ", {"TEST_KNOWLEDGE_AI_KEY": "secret-key"}):
            first = generate_proposals(document, requested_by=self.member)
            second = generate_proposals(document, requested_by=self.member)

        self.assertEqual(KnowledgeProposalRun.objects.filter(document=document).count(), 2)
        self.assertEqual(KnowledgeProposal.objects.filter(document=document).count(), 6)
        self.assertEqual(
            set(
                KnowledgeProposal.objects.filter(
                    pk__in=[item.pk for item in first]
                ).values_list("status", flat=True)
            ),
            {KnowledgeProposal.STATUS_STALE},
        )
        self.assertTrue(
            all(item.status == KnowledgeProposal.STATUS_PENDING for item in second)
        )
        category_proposal = next(
            item for item in second if item.proposal_type == KnowledgeProposal.TYPE_CATEGORY
        )
        tag_proposal = next(
            item for item in second if item.proposal_type == KnowledgeProposal.TYPE_TAGS
        )
        self.assertFalse(category_proposal.suggested_value["is_new"])
        self.assertEqual(tag_proposal.suggested_value["new_items"], ["现金流"])
        self.assertFalse(KnowledgeTag.objects.filter(name="现金流").exists())
        self.assertEqual(first[0].run.sequence, 1)
        self.assertEqual(second[0].run.sequence, 2)
        self.assertEqual(second[0].run.analysis_request.provider, provider)

        accepted = self.client.post(
            reverse("knowledge:proposal_review", kwargs={"pk": tag_proposal.pk}),
            {"action": "accept", "value": "长期主义，现金流"},
        )
        self.assertRedirects(
            accepted,
            reverse("knowledge:document_detail", kwargs={"pk": document.pk}),
        )
        self.assertTrue(KnowledgeTag.objects.filter(name="现金流").exists())
        detail = self.client.get(
            reverse("knowledge:document_detail", kwargs={"pk": document.pk})
        )
        self.assertContains(detail, "本次整理建议")
        self.assertContains(detail, "AI 整理历史")
        self.assertContains(detail, "第 1 次")

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
        self.assertEqual(request.scope["cloud_ai_consent"], "source")
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

    def test_knowledge_ai_does_not_reuse_ipo_vision_provider(self):
        source = self.make_source(suffix="text-provider")
        text_provider = self.make_text_ai_provider()
        AiProvider.objects.create(
            name="IPO 视觉识别",
            provider_type="openai_compatible",
            base_url="https://vision.example.com/v1",
            model_name="vision-only-model",
            extra_data={"usage": "ipo_image_recognition"},
        )

        self.assertEqual(knowledge_ai_provider(source), text_provider)

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
        proposal_run = KnowledgeProposalRun.objects.create(
            document=document,
            revision=document.current_revision,
            sequence=1,
            requested_by=self.member,
            model_name="test-model",
            prompt_version="knowledge-organize-v1",
            content_hash=document.current_revision.content_hash,
        )
        proposal = KnowledgeProposal.objects.create(
            document=document,
            revision=document.current_revision,
            run=proposal_run,
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
            include_in_knowledge=True,
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

    def test_stale_ai_job_returns_selected_document_to_unorganized(self):
        source = self.make_source(suffix="stale-ai", allow_cloud_ai=True)
        document = self.make_pending_document(
            source=source,
            title="超时的 AI 整理资料",
            external_id="stale-ai-page",
        )
        document.curation_status = KnowledgeDocument.CURATION_PENDING_AI
        document.save(update_fields=["curation_status", "updated_at"])
        index_document(document)
        stale = KnowledgeJob.objects.create(
            family=self.family,
            source=source,
            requested_by=self.member,
            job_type=KnowledgeJob.TYPE_GENERATE_PROPOSALS,
            status=KnowledgeJob.STATUS_RUNNING,
            parameters={"document_ids": [document.pk]},
            started_at=timezone.now() - timedelta(hours=1),
            heartbeat_at=timezone.now() - timedelta(hours=1),
        )

        self.assertEqual(recover_stale_jobs(), 1)

        stale.refresh_from_db()
        document.refresh_from_db()
        self.assertEqual(stale.status, KnowledgeJob.STATUS_FAILED)
        self.assertEqual(
            document.curation_status,
            KnowledgeDocument.CURATION_NORMALIZED,
        )
        self.assertEqual(
            KnowledgeSearchEntry.objects.get(document=document).curation_status,
            KnowledgeDocument.CURATION_NORMALIZED,
        )

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
