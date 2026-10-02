from pathlib import Path
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone
from ai_analysis.models import AiProvider
from reading import ai
from reading.models import Annotation, AnnotationComment, Book, BookLifecycleEvent, ReadingPlan, ReadingPlanItem, ReadingPosition
from reading.services import process_file
from reading.storage import storage
from .test_reading import ReadingTests, epub_bytes


class RecycleTests(TestCase):
    setUp = ReadingTests.setUp
    member = ReadingTests.member
    upload = ReadingTests.upload
    url = ReadingTests.url
    payload = ReadingTests.payload

    def test_delete_restore_preserves_data_and_hides_all_reading_routes(self):
        book = self.upload(visibility=Book.FAMILY)
        process_file(book.file.pk)
        self.client.post(self.url("position", book), self.payload(book), content_type="application/json")
        note = Annotation.objects.create(book=book, author=self.peer, file_hash=book.file.sha256,
            normalizer_version=book.file.normalizer_version, anchor={"section": 0, "cfi": "epubcfi(/6/2!/4/2:0)"},
            quote="阅读测试文本。", note="PRIVATE_PEER_NOTE", visibility=Book.PRIVATE)
        AnnotationComment.objects.create(annotation=note, author=self.peer, body="保留讨论")
        plan = ReadingPlan.objects.create(member=self.owner, title="我的测试计划", target_date=timezone.now().date())
        ReadingPlanItem.objects.create(plan=plan, book=book, title=book.title, kind="online")
        original = Path(storage().path(book.file.original_path)).read_bytes()
        preview = self.client.get(self.url("delete", book))
        self.assertContains(preview, "我的测试计划")
        self.assertNotContains(preview, "PRIVATE_PEER_NOTE")
        book.refresh_from_db();self.assertIsNone(book.deleted_at)
        self.client.post(self.url("delete", book), {})
        book.refresh_from_db();self.assertIsNone(book.deleted_at)
        self.client.post(self.url("delete", book), {"confirmed": "yes"})
        self.client.post(self.url("delete", book), {"confirmed": "yes"})
        book.refresh_from_db();self.assertIsNotNone(book.deleted_at)
        self.assertEqual(BookLifecycleEvent.objects.count(), 1)
        self.assertEqual(ReadingPosition.objects.count(), 1)
        self.assertEqual(AnnotationComment.objects.count(), 1)
        self.assertEqual(ReadingPlanItem.objects.count(), 1)
        self.assertEqual(Path(storage().path(book.file.original_path)).read_bytes(), original)
        for member in [self.owner, self.peer, self.other]:
            self.client.force_login(member.user)
            for name in ["detail", "reader", "file", "manifest", "resource", "position", "annotations", "edit", "ai_create"]:
                self.assertEqual(self.client.get(self.url(name, book)).status_code, 404, name)
            self.assertEqual(self.client.get(reverse("reading:note", args=[note.pk])).status_code, 404)
            self.assertNotContains(self.client.get(reverse("reading:index")), book.title)
        self.client.force_login(self.peer.user)
        self.assertNotContains(self.client.get(reverse("reading:recycle")), book.title)
        self.assertEqual(self.client.post(self.url("restore", book)).status_code, 404)
        self.client.force_login(self.owner.user)
        self.assertContains(self.client.get(reverse("reading:recycle")), book.title)
        self.assertEqual(self.client.get(self.url("restore", book)).status_code, 405)
        self.client.post(self.url("restore", book));self.client.post(self.url("restore", book))
        book.refresh_from_db();self.assertIsNone(book.deleted_at)
        self.assertEqual(BookLifecycleEvent.objects.count(), 2)
        self.assertEqual(self.client.get(self.url("reader", book)).status_code, 200)
        self.client.force_login(self.peer.user)
        self.assertContains(self.client.get(reverse("reading:note", args=[note.pk])), "PRIVATE_PEER_NOTE")

    def test_only_owner_can_delete_and_csrf_is_required(self):
        book = self.upload(visibility=Book.FAMILY)
        self.peer.role = "admin";self.peer.save()
        for member in [self.peer, self.other]:
            self.client.force_login(member.user)
            self.assertEqual(self.client.get(self.url("delete", book)).status_code, 404)
            self.assertEqual(self.client.post(self.url("delete", book), {"confirmed": "yes"}).status_code, 404)
        csrf_client = Client(enforce_csrf_checks=True);csrf_client.force_login(self.owner.user)
        self.assertEqual(csrf_client.post(self.url("delete", book), {"confirmed": "yes"}).status_code, 403)
        self.owner.role = "viewer";self.owner.save();self.client.force_login(self.owner.user)
        self.assertEqual(self.client.post(self.url("delete", book), {"confirmed": "yes"}).status_code, 403)

    def test_recycled_duplicate_upload_and_import_resume(self):
        book = self.upload()
        self.client.post(self.url("delete", book), {"confirmed": "yes"})
        self.assertFalse(process_file(book.file.pk))
        duplicate = self.client.post(reverse("reading:upload"), {"title": "重复文件", "visibility": "private",
            "file": SimpleUploadedFile("sample.epub", epub_bytes())})
        self.assertRedirects(duplicate, reverse("reading:recycle"))
        self.assertEqual(Book.objects.count(), 1)
        self.client.post(self.url("restore", book))
        self.assertTrue(process_file(book.file.pk))

    def test_running_ai_blocks_delete_and_queued_job_is_cancelled(self):
        book = self.upload();process_file(book.file.pk);book.refresh_from_db()
        provider = AiProvider.objects.create(name="模拟服务商", provider_type="openai_compatible",
            model_name="test-model", base_url="https://example.com/v1")
        job = ai.draft(book, self.owner, provider, {"kind": "chapter", "section": 0})
        type(job).objects.filter(pk=job.pk).update(status="running")
        self.client.post(self.url("delete", book), {"confirmed": "yes"})
        book.refresh_from_db();self.assertIsNone(book.deleted_at)
        type(job).objects.filter(pk=job.pk).update(status="queued", confirmed_at=timezone.now())
        self.client.post(self.url("delete", book), {"confirmed": "yes"})
        job.refresh_from_db();self.assertEqual(job.status, "cancelled")
        with patch("reading.ai.request_completion") as send:
            self.assertFalse(ai.process_job(job.pk));send.assert_not_called()
        self.client.post(self.url("restore", book))
        self.assertFalse(ai.process_job(job.pk))
