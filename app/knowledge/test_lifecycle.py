import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.test import TestCase, TransactionTestCase
from django.db import connection, close_old_connections
from django.urls import reverse
from django.utils import timezone

from ai_analysis.models import AiAnalysisRequest, AiAnalysisResult
from ai_analysis.read_tools import GlobalAiReadError, knowledge_revision
from family_core.models import Family, FamilyMember
from .ai import KnowledgeAiError, generate_proposals
from .lifecycle import change_document, process_cleanup, request_cleanup, validate_file
from .models import (KnowledgeArtifact, KnowledgeArtifactEvidence, KnowledgeArtifactVersion,
    KnowledgeAsset, KnowledgeCurationRevision, KnowledgeDocument, KnowledgeFileCleanup,
    KnowledgeJob, KnowledgeSource, KnowledgeRevision, KnowledgeWebCapture)
from .permissions import accessible_documents, accessible_search_entries
from .search import index_document, rebuild_family_search
from .services import _sync_page
from .storage import protected_knowledge_storage
from .web_capture import queue_capture
from .web_fetch import WebCaptureError


class DocumentLifecycleTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.storage_override = self.settings(KNOWLEDGE_FILE_ROOT=self.tmp.name)
        self.storage_override.enable()
        self.addCleanup(self.storage_override.disable)
        storage_patch = patch.object(protected_knowledge_storage, "location", self.tmp.name)
        storage_patch.start()
        self.addCleanup(storage_patch.stop)
        self.family = Family.objects.create(name="测试家庭")
        self.owner = self.member("owner", "member")
        self.admin = self.member("admin", "admin")
        self.other = self.member("other", "member")
        self.viewer = self.member("viewer", "viewer")
        self.source = KnowledgeSource.objects.create(family=self.family, owner=self.owner, key="test-doc", name="测试来源", kind="web_capture", visibility="family")
        self.doc = KnowledgeDocument.objects.create(family=self.family, owner=self.owner, source=self.source, external_id="test-doc", title="生命周期测试正文", visibility="family", knowledge_status="included", library_tier="knowledge", curation_status="confirmed", confirmed_summary="人工确认的摘要", category="测试分类", tags=["测试"])
        self.old = self.revision(1)
        self.current = self.revision(2)
        self.doc.current_revision = self.current
        self.doc.save()
        index_document(self.doc)
        self.client.force_login(self.owner.user)

    def member(self, name, role, family=None):
        user = get_user_model().objects.create_user(username=name, password="disposable-test-only")
        return FamilyMember.objects.create(user=user, family=family or self.family, display_name=name, role=role)

    def revision(self, number):
        revision = KnowledgeRevision.objects.create(document=self.doc, revision_number=number, content_hash=str(number)*64, normalized_html=f"<p>测试正文 {number}</p>", plain_text=f"测试正文 {number}", converter_version="test")
        revision.raw_file.save("page.html", ContentFile(f"正文 {number}".encode()), save=True)
        return revision

    def cite(self, revision):
        artifact = KnowledgeArtifact.objects.create(family=self.family, owner=self.other, title="专题", artifact_type="manual", visibility="private")
        version = KnowledgeArtifactVersion.objects.create(artifact=artifact, version_number=1, content_hash="a"*64, byte_size=0, created_by=self.other)
        return KnowledgeArtifactEvidence.objects.create(version=version, reference_key="test", citation_text="引用", citation_title="原文", status="matched", document=self.doc, revision=revision)

    def url(self):
        return reverse("knowledge:document_manage", args=[self.doc.pk])

    def test_gets_do_not_write_or_delete(self):
        baseline = (self.doc.updated_at, self.doc.lifecycle_events.count(), KnowledgeFileCleanup.objects.count())
        for url in [self.url(), reverse("knowledge:trash"), reverse("knowledge:document_detail", args=[self.doc.pk])]:
            self.assertEqual(self.client.get(url).status_code, 200)
        self.doc.refresh_from_db()
        self.assertEqual(baseline, (self.doc.updated_at, self.doc.lifecycle_events.count(), KnowledgeFileCleanup.objects.count()))
        self.assertTrue(self.old.raw_file.storage.exists(self.old.raw_file.name))

    def test_trash_excludes_lists_search_review_ai_assets_and_raw(self):
        asset = KnowledgeAsset.objects.create(revision=self.current, external_id="image", file="", mime_type="image/png", byte_size=4)
        asset.file.save("a.png", ContentFile(b"test"), save=True)
        change_document(self.doc.pk, self.owner, "trash")
        self.assertFalse(accessible_documents(self.owner).filter(pk=self.doc.pk).exists())
        self.assertFalse(accessible_search_entries(self.owner).filter(document=self.doc).exists())
        for url in [reverse("knowledge:document_detail", args=[self.doc.pk]), reverse("knowledge:revision_raw_download", args=[self.current.pk]), reverse("knowledge:asset_download", args=[asset.pk]), reverse("knowledge:document_organize", args=[self.doc.pk])]:
            self.assertEqual(self.client.get(url).status_code, 404)
        with self.assertRaises(GlobalAiReadError):
            knowledge_revision(self.owner, document_id=self.doc.pk, revision_id=self.current.pk)
        with self.assertRaises(KnowledgeAiError):
            generate_proposals(self.doc)
        self.assertTrue(asset.file.storage.exists(asset.file.name))
        self.assertContains(self.client.get(reverse("knowledge:trash")), self.doc.title)

    def test_stale_index_and_rebuild_cannot_resurrect_trash(self):
        stale = KnowledgeDocument.objects.get(pk=self.doc.pk)
        change_document(self.doc.pk, self.owner, "trash")
        self.assertIsNone(index_document(stale))
        self.assertEqual(rebuild_family_search(self.family)["documents"], 0)
        self.assertFalse(accessible_search_entries(self.owner).filter(document=self.doc).exists())

    def test_restore_keeps_state_summary_and_versions(self):
        change_document(self.doc.pk, self.owner, "trash")
        result = change_document(self.doc.pk, self.owner, "restore")
        self.assertEqual((result.library_tier, result.knowledge_status, result.curation_status, result.confirmed_summary, result.tags), ("knowledge", "included", "confirmed", "人工确认的摘要", ["测试"]))
        self.assertEqual(result.revisions.count(), 2)
        self.assertTrue(accessible_search_entries(self.owner).filter(document=result).exists())
        self.assertEqual(list(result.lifecycle_events.values_list("action", flat=True)), ["restore", "trash"])

    def test_unfeature_preserves_body_and_human_result(self):
        result = change_document(self.doc.pk, self.owner, "unfeature")
        self.assertEqual((result.library_tier, result.knowledge_status, result.curation_status), ("archive", "included", "normalized"))
        self.assertEqual(result.confirmed_summary, "人工确认的摘要")
        self.assertEqual(result.current_revision_id, self.current.pk)
        self.assertEqual(result.revisions.count(), 2)

    def test_other_member_viewer_cross_family_cannot_manage(self):
        outsiders = [self.other, self.viewer, self.member("alien", "admin", Family.objects.create(name="其他家庭"))]
        for member in outsiders:
            with self.subTest(member=member.pk), self.assertRaises(ValidationError):
                change_document(self.doc.pk, member, "trash")
            self.client.force_login(member.user)
            self.assertIn(self.client.get(self.url()).status_code, [403, 404])

    def test_admin_cannot_read_or_delete_private_document(self):
        self.doc.visibility = self.source.visibility = "private"
        self.doc.save(); self.source.save()
        self.client.force_login(self.admin.user)
        self.assertEqual(self.client.get(self.url()).status_code, 404)
        with self.assertRaises(ValidationError):
            change_document(self.doc.pk, self.admin, "trash")

    def test_admin_can_manage_shared_document(self):
        self.assertIsNotNone(change_document(self.doc.pk, self.admin, "trash").trashed_at)

    def test_active_source_tasks_block_all_lifecycle_actions(self):
        for status in KnowledgeJob.ACTIVE_STATUSES:
            job = KnowledgeJob.objects.create(family=self.family, source=self.source, job_type="sync_source", status=status)
            for action in ["trash", "unfeature"]:
                with self.assertRaises(ValidationError):
                    change_document(self.doc.pk, self.owner, action)
            with self.assertRaises(ValidationError):
                request_cleanup(self.doc.pk, self.owner)
            job.delete()

    def test_active_capture_without_source_blocks_trash(self):
        job = KnowledgeJob.objects.create(family=self.family, job_type="capture_web")
        KnowledgeWebCapture.objects.create(family=self.family, owner=self.owner, url="https://example.com/", url_hash="c"*64, document=self.doc, last_job=job)
        with self.assertRaises(ValidationError):
            change_document(self.doc.pk, self.owner, "trash")

    def test_capture_cannot_queue_after_trash_or_purge(self):
        capture = KnowledgeWebCapture.objects.create(family=self.family, owner=self.owner, url="https://example.com/", url_hash="c"*64, document=self.doc)
        change_document(self.doc.pk, self.owner, "trash")
        with self.assertRaises(WebCaptureError):
            queue_capture(capture, "recapture")
        request_cleanup(self.doc.pk, self.owner, whole_document=True)
        with self.assertRaises(WebCaptureError):
            queue_capture(capture, "recapture")

    def test_sync_never_downloads_deleted_document(self):
        change_document(self.doc.pk, self.owner, "trash")
        client = Mock()
        result, status = _sync_page(client, self.source, {"id": "section"}, {"id": "test-doc", "title": "新标题"})
        self.assertEqual(status, "skipped")
        client.page_content.assert_not_called()
        self.assertEqual(result.title, self.doc.title)

    def test_cleanup_retains_current_files_summary_and_version_numbers(self):
        name = self.old.raw_file.name
        task = request_cleanup(self.doc.pk, self.owner)
        self.old.refresh_from_db(); self.doc.refresh_from_db()
        self.assertTrue(self.old.purged_at)
        self.assertEqual(self.old.plain_text, "")
        self.assertTrue(Path(self.tmp.name, name).exists())  # DB commit precedes filesystem cleanup.
        self.assertTrue(process_cleanup(task.pk))
        self.assertFalse(Path(self.tmp.name, name).exists())
        self.assertTrue(self.current.raw_file.storage.exists(self.current.raw_file.name))
        self.assertEqual(self.doc.confirmed_summary, "人工确认的摘要")
        newer = self.revision(3)
        self.assertEqual(newer.revision_number, 3)
        self.assertEqual(self.client.get(reverse("knowledge:revision_raw_download", args=[self.old.pk])).status_code, 404)

    def test_referenced_private_evidence_blocks_old_version_cleanup(self):
        self.cite(self.old)
        with self.assertRaises(ValidationError):
            request_cleanup(self.doc.pk, self.owner)
        self.assertContains(self.client.get(self.url()), "被专题引用")
        self.assertNotContains(self.client.get(self.url()), "专题</")  # No private artifact title.

    def test_referenced_document_can_trash_restore_but_not_purge(self):
        self.cite(self.old)
        change_document(self.doc.pk, self.owner, "trash")
        with self.assertRaises(ValidationError):
            request_cleanup(self.doc.pk, self.owner, whole_document=True)
        change_document(self.doc.pk, self.owner, "restore")
        self.assertEqual(self.doc.revisions.count(), 2)

    def test_purge_requires_trash_and_exact_confirmation(self):
        with self.assertRaises(ValidationError):
            request_cleanup(self.doc.pk, self.owner, whole_document=True)
        change_document(self.doc.pk, self.owner, "trash")
        self.client.post(self.url(), {"action": "purge", "confirmation": "yes"})
        self.doc.refresh_from_db()
        self.assertIsNone(self.doc.purged_at)
        self.assertEqual(KnowledgeFileCleanup.objects.count(), 0)

    def test_old_versions_require_confirmation(self):
        self.client.post(self.url(), {"action": "versions"})
        self.old.refresh_from_db()
        self.assertIsNone(self.old.purged_at)

    def test_permanent_deletion_scrubs_text_keeps_identity_and_audit(self):
        KnowledgeCurationRevision.objects.create(document=self.doc, sequence=1, summary="旧确认摘要", change_type="manual")
        request = AiAnalysisRequest.objects.create(family=self.family, member=self.owner, module="knowledge", prompt="私人文本", sanitized_input={"title": "秘密"}, scope={"document_id": self.doc.pk, "revision_id": self.current.pk}, status="failed")
        result = AiAnalysisResult.objects.create(request=request, result_text="私人结果", result_json={"text": "私人结果"}, tokens_used=77)
        change_document(self.doc.pk, self.owner, "trash")
        task = request_cleanup(self.doc.pk, self.owner, whole_document=True)
        self.assertTrue(process_cleanup(task.pk))
        self.doc.refresh_from_db(); request.refresh_from_db(); result.refresh_from_db()
        self.assertEqual((self.doc.external_id, self.doc.source_id), ("test-doc", self.source.pk))
        self.assertEqual(self.doc.title, "已彻底删除的资料")
        self.assertEqual(self.doc.confirmed_summary, "")
        self.assertFalse(self.doc.curation_revisions.exists())
        self.assertEqual((request.prompt, request.scope, result.result_text, result.tokens_used), ("", {}, "", 77))
        self.assertTrue(self.doc.lifecycle_events.filter(action="purge").exists())
        self.assertFalse(accessible_documents(self.owner, include_trashed=True).filter(pk=self.doc.pk).exists())
        self.assertNotContains(self.client.get(reverse("knowledge:trash")), "生命周期测试正文")
        with self.assertRaises(ValidationError):
            change_document(self.doc.pk, self.owner, "restore")

    def test_cleanup_failure_can_retry_idempotently(self):
        task = request_cleanup(self.doc.pk, self.owner)
        with patch.object(protected_knowledge_storage, "delete", side_effect=OSError("test")):
            self.assertFalse(process_cleanup(task.pk))
        task.refresh_from_db()
        self.assertEqual(task.status, "failed")
        self.assertContains(self.client.get(reverse("knowledge:trash")), "文件清理需要重试")
        self.assertTrue(process_cleanup(task.pk))
        self.assertTrue(process_cleanup(task.pk))
        task.refresh_from_db()
        self.assertEqual((task.status, task.files), ("success", []))

    def test_cleanup_rejects_other_document_or_unsafe_path(self):
        for name in ["../outside", "backups/page.html", "families/1/sources/1/documents/999/revisions/1/raw/a.html"]:
            with self.assertRaises(ValidationError):
                validate_file(self.doc, name)
        self.old.raw_file = "../outside"; self.old.save()
        with self.assertRaises(ValidationError):
            request_cleanup(self.doc.pk, self.owner)
        self.old.refresh_from_db()
        self.assertIsNone(self.old.purged_at)
        self.assertFalse(KnowledgeFileCleanup.objects.exists())

    def test_cleanup_checks_all_shared_files_before_deleting_any(self):
        name = self.old.raw_file.name
        task = request_cleanup(self.doc.pk, self.owner)
        self.current.raw_file = name; self.current.save()
        self.assertFalse(process_cleanup(task.pk))
        self.assertTrue(Path(self.tmp.name, name).exists())

    def test_reading_original_is_never_deleted(self):
        self.old.raw_file = "reading/artifacts/example.html"; self.old.save()
        task = request_cleanup(self.doc.pk, self.owner)
        self.assertEqual(task.files, [])
        self.assertTrue(process_cleanup(task.pk))

    def test_purged_hash_can_be_captured_again_as_new_number(self):
        request_cleanup(self.doc.pk, self.owner)
        new = KnowledgeRevision.objects.create(document=self.doc, revision_number=3, content_hash=self.old.content_hash)
        self.assertNotEqual(new.pk, self.old.pk)

    def test_detail_has_management_entry(self):
        self.assertContains(self.client.get(reverse("knowledge:document_detail", args=[self.doc.pk])), "打开资料管理")

    def test_confirmation_post_really_cleans_only_old_versions(self):
        response = self.client.post(self.url(), {"action": "versions", "confirmation": "清理旧版本"})
        self.assertEqual(response.status_code, 302)
        self.old.refresh_from_db(); self.current.refresh_from_db()
        self.assertIsNotNone(self.old.purged_at)
        self.assertIsNone(self.current.purged_at)

    def test_confirmation_post_really_purges_only_trash(self):
        self.client.post(self.url(), {"action": "trash"})
        response = self.client.post(self.url(), {"action": "purge", "confirmation": "彻底删除"})
        self.assertEqual(response.status_code, 302)
        self.doc.refresh_from_db()
        self.assertTrue(self.doc.purged_at)
        self.assertContains(self.client.get(self.url()), "不能恢复")

    def test_partial_cleanup_preserves_referenced_version_and_cleans_other_old(self):
        self.cite(self.old)
        extra = self.revision(3)
        task = request_cleanup(self.doc.pk, self.owner)
        self.assertTrue(process_cleanup(task.pk))
        extra.refresh_from_db(); self.old.refresh_from_db()
        self.assertTrue(extra.purged_at)
        self.assertIsNone(self.old.purged_at)
        self.assertEqual(self.doc.current_revision_id, self.current.pk)

    def test_restore_preserves_pending_and_archive_states(self):
        for state in ["pending", "included", "archived"]:
            self.doc.knowledge_status = state
            self.doc.library_tier = "archive"
            self.doc.save()
            change_document(self.doc.pk, self.owner, "trash")
            restored = change_document(self.doc.pk, self.owner, "restore")
            self.assertEqual((restored.knowledge_status, restored.library_tier), (state, "archive"))
            self.doc.refresh_from_db()

    def test_old_ai_content_cleared_but_current_human_summary_retained(self):
        from .models import KnowledgeProposal, KnowledgeProposalRun
        request = AiAnalysisRequest.objects.create(family=self.family, member=self.owner, module="knowledge", prompt="旧建议", status="success")
        result = AiAnalysisResult.objects.create(request=request, result_text="旧 AI 内容", tokens_used=10)
        run = KnowledgeProposalRun.objects.create(document=self.doc, revision=self.old, sequence=1, analysis_request=request, model_name="test", prompt_version="test", content_hash=self.old.content_hash)
        KnowledgeProposal.objects.create(document=self.doc, revision=self.old, run=run, proposal_type="summary", suggested_value={"text": "旧 AI 内容"}, model_name="test", prompt_version="test", content_hash=self.old.content_hash)
        human = KnowledgeCurationRevision.objects.create(document=self.doc, sequence=1, summary="已确认的结果", proposal_run=run, change_type="ai_confirmed")
        task = request_cleanup(self.doc.pk, self.owner)
        self.assertTrue(process_cleanup(task.pk))
        result.refresh_from_db(); human.refresh_from_db(); self.doc.refresh_from_db()
        self.assertEqual((result.result_text, result.tokens_used), ("", 10))
        self.assertEqual(human.summary, "已确认的结果")
        self.assertIsNone(human.proposal_run_id)
        self.assertEqual(self.doc.confirmed_summary, "人工确认的摘要")

    def test_retry_cannot_access_other_documents_task(self):
        task = request_cleanup(self.doc.pk, self.owner)
        self.client.force_login(self.other.user)
        self.assertEqual(self.client.post(self.url(), {"action": "retry_files", "task_id": task.pk}).status_code, 403)

    def test_csrf_required_for_deletion(self):
        from django.test import Client
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owner.user)
        self.assertEqual(client.post(self.url(), {"action": "trash"}).status_code, 403)

    def test_invalid_cleanup_record_is_rejected_without_server_error(self):
        for value in ["", "abc", "-1", "1" * 100]:
            response = self.client.post(self.url(), {"action": "retry_files", "task_id": value}, follow=True)
            self.assertContains(response, "文件清理记录无效")
        self.doc.refresh_from_db()
        self.assertIsNone(self.doc.purged_at)


class LifecycleConcurrencyTests(TransactionTestCase):
    def test_capture_enqueue_and_trash_cannot_both_succeed(self):
        if connection.vendor != "postgresql":
            self.skipTest("requires PostgreSQL row locks")
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        family = Family.objects.create(name="并发测试家庭")
        user = get_user_model().objects.create_user(username="lifecycle-concurrent")
        owner = FamilyMember.objects.create(user=user, family=family, display_name="测试成员")
        source = KnowledgeSource.objects.create(family=family, owner=owner, key="concurrent", name="测试来源", kind="web_capture")
        document = KnowledgeDocument.objects.create(family=family, owner=owner, source=source, external_id="concurrent", title="并发测试")
        capture = KnowledgeWebCapture.objects.create(family=family, owner=owner, document=document, url="https://example.com/", url_hash="d"*64)
        barrier = Barrier(2)
        def enqueue():
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                queue_capture(capture)
                return True
            except WebCaptureError:
                return False
            finally:
                close_old_connections()
        def trash_document():
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                change_document(document.pk, owner, "trash")
                return True
            except ValidationError:
                return False
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as executor:
            queued, trashed = executor.submit(enqueue), executor.submit(trash_document)
            self.assertNotEqual(queued.result(timeout=20), trashed.result(timeout=20))
        document.refresh_from_db(); capture.refresh_from_db()
        self.assertEqual(bool(document.trashed_at), capture.last_job_id is None)
