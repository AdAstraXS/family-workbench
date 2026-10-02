from pathlib import Path
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, TestCase
from django.urls import reverse

from reading import ai
from reading.artifacts import publish, archive_version, export_html
from reading.models import (Book, BookPurgeTask, Annotation, AnnotationComment,
    ReadingPlan, ReadingPlanItem, ReadingPosition, ReadingArtifact, ReadingArtifactVersion, ReadingAiJob)
from reading.purge import process_purge, request_purge
from reading.services import process_file, change_book_deleted_state
from reading.storage import storage
from ai_analysis.models import AiProvider, AiAnalysisRequest, AiAnalysisResult
from knowledge.models import KnowledgeRevision
from .test_reading import ReadingTests
from .test_collaboration import structured


class PurgeTests(TestCase):
    setUp = ReadingTests.setUp
    member = ReadingTests.member
    upload = ReadingTests.upload
    url = ReadingTests.url
    payload = ReadingTests.payload

    def recycled(self):
        book = self.upload(visibility=Book.FAMILY)
        process_file(book.file.pk)
        book.refresh_from_db()
        change_book_deleted_state(book, self.owner, True)
        book.refresh_from_db()
        return book

    def test_full_purge_keeps_archived_file_and_other_book(self):
        book = self.upload(visibility=Book.FAMILY)
        process_file(book.file.pk); book.refresh_from_db()
        other = self.upload(name="other.txt", data=b"other book")
        other_path = Path(storage().path(other.file.original_path))
        note = Annotation.objects.create(book=book, author=self.peer, file_hash=book.file.sha256,
            normalizer_version=book.file.normalizer_version, anchor={}, quote="private quote", note="private note")
        AnnotationComment.objects.create(annotation=note, author=self.peer, body="private discussion")
        ReadingPosition.objects.create(book=book, member=self.peer, file_hash=book.file.sha256)
        plan = ReadingPlan.objects.create(member=self.peer, title="保留整个计划", target_date="2026-10-10")
        ReadingPlanItem.objects.create(plan=plan, book=book, title=book.title, kind="online")
        ReadingPlanItem.objects.create(plan=plan, book=other, title=other.title, kind="online")
        data = structured()
        version, _ = publish(book, self.peer, data["title"], "private", export_html(data), data)
        document = archive_version(version, self.peer)
        archived_path = Path(storage().path(version.original_path))
        archived_bytes = archived_path.read_bytes()
        data2 = structured(); data2["title"] = "未归档成果"
        version2, _ = publish(book, self.owner, data2["title"], "private", export_html(data2), data2)
        disposable = Path(storage().path(version2.original_path))
        provider = AiProvider.objects.create(name="test", provider_type="openai_compatible", model_name="test", base_url="https://example.com/v1")
        job = ai.draft(book, self.owner, provider, {"kind":"chapter", "section":0})
        audit = AiAnalysisRequest.objects.create(family=self.family, member=self.owner, module="reading", prompt="quote", sanitized_input={"quote":"private"})
        AiAnalysisResult.objects.create(request=audit, result_json={"quote":"private"}, tokens_used=123)
        job.analysis_request = audit; job.save()
        change_book_deleted_state(book, self.owner, True)
        book_dir = Path(storage().path(str(book.pk)))
        before = self.client.get(self.url("purge", book))
        self.assertNotContains(before, "private note")
        response = self.client.post(self.url("purge", book), {"book_title":book.title, "confirmed":"yes"})
        self.assertRedirects(response, reverse("reading:recycle"))
        self.assertFalse(Book.objects.filter(pk=book.pk).exists())
        for model in [Annotation, AnnotationComment, ReadingPosition, ReadingArtifact, ReadingArtifactVersion, ReadingAiJob]:
            self.assertEqual(model.objects.count(), 0, model)
        self.assertTrue(ReadingPlan.objects.filter(pk=plan.pk).exists())
        self.assertEqual(plan.items.count(), 1)
        self.assertFalse(book_dir.exists()); self.assertFalse(disposable.exists())
        self.assertEqual(archived_path.read_bytes(), archived_bytes)
        self.assertEqual(other_path.read_bytes(), b"other book")
        document.refresh_from_db()
        revision = document.current_revision
        self.assertTrue((Path(self.tmp.name) / str(revision.raw_file)).exists())
        self.assertIn("此归档版本独立保留", revision.normalized_html)
        self.assertNotIn('/reading/artifacts/', revision.normalized_html)
        audit.refresh_from_db(); self.assertEqual(audit.prompt, ""); self.assertEqual(audit.sanitized_input, {})
        audit.result.refresh_from_db(); self.assertEqual(audit.result.tokens_used, 123); self.assertEqual(audit.result.result_json, {})
        self.assertEqual(BookPurgeTask.objects.get().status, "success")
        # A genuinely deleted file can be uploaded afresh.
        self.assertNotEqual(self.upload().pk, book.pk)

    def test_confirmation_owner_csrf_and_recycle_only(self):
        book = self.upload()
        self.assertEqual(self.client.get(self.url("purge", book)).status_code, 404)
        change_book_deleted_state(book, self.owner, True)
        for fields in [{}, {"confirmed":"yes", "book_title":"wrong"}, {"book_title":book.title}]:
            self.client.post(self.url("purge", book), fields)
            self.assertTrue(Book.objects.filter(pk=book.pk).exists())
        for member in [self.peer, self.other]:
            self.client.force_login(member.user)
            self.assertEqual(self.client.get(self.url("purge", book)).status_code, 404)
            self.assertEqual(self.client.post(self.url("purge", book), {"confirmed":"yes", "book_title":book.title}).status_code, 404)
        csrf = Client(enforce_csrf_checks=True); csrf.force_login(self.owner.user)
        self.assertEqual(csrf.post(self.url("purge", book), {"confirmed":"yes", "book_title":book.title}).status_code, 403)

    def test_cleanup_failure_is_visible_retryable_and_command_fails(self):
        book = self.recycled()
        with patch("pathlib.Path.unlink", side_effect=PermissionError("test failure")):
            self.client.post(self.url("purge", book), {"confirmed":"yes", "book_title":book.title})
            task = BookPurgeTask.objects.get()
            self.assertEqual(task.status, "failed")
            self.assertContains(self.client.get(reverse("reading:recycle")), "重试文件清理")
            with self.assertRaises(CommandError): call_command("process_reading_imports")
        self.client.force_login(self.peer.user)
        self.assertNotContains(self.client.get(reverse("reading:recycle")), task.title)
        self.assertEqual(self.client.post(reverse("reading:purge_retry", args=[task.pk])).status_code, 404)
        self.client.force_login(self.owner.user)
        self.client.post(reverse("reading:purge_retry", args=[task.pk]))
        task.refresh_from_db(); self.assertEqual(task.status, "success")
        self.assertTrue(process_purge(task.pk))
        self.assertFalse(Path(storage().path(str(task.book_id))).exists())

    def test_path_boundary_and_running_import_block_purge(self):
        book = self.recycled()
        type(book.file).objects.filter(pk=book.file.pk).update(status="processing")
        with self.assertRaises(ValidationError): request_purge(book, self.owner)
        type(book.file).objects.filter(pk=book.file.pk).update(status="ready", original_path="../outside.txt")
        with self.assertRaises(ValidationError): request_purge(book, self.owner)
        self.assertTrue(Book.objects.filter(pk=book.pk).exists())
        self.assertFalse(BookPurgeTask.objects.exists())
        task = BookPurgeTask.objects.create(book_id=book.pk, owner=self.owner, title="invalid", directories=[{"name":"../", "preserve":[]}])
        self.assertFalse(process_purge(task.pk))
        root = Path(storage().location)
        self.assertTrue(root.exists())
        self.assertTrue(Path(storage().path(str(book.pk))).exists())

    def test_symlink_is_rejected_before_any_content_is_removed(self):
        book = self.recycled()
        outside = Path(self.tmp.name) / "outside.txt"
        outside.write_text("keep", encoding="utf-8")
        link = Path(storage().path(f"{book.pk}/unsafe"))
        link.symlink_to(outside)
        with self.assertRaises(ValidationError): request_purge(book, self.owner)
        self.assertTrue(Book.objects.filter(pk=book.pk).exists())
        self.assertEqual(outside.read_text(encoding="utf-8"), "keep")
        self.assertTrue(Path(storage().path(book.file.original_path)).exists())
