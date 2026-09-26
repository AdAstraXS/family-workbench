import io
import json
import tempfile
import zipfile
from pathlib import Path
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from family_core.models import Family, FamilyMember
from reading.importer import ImportFailure, normalize_epub, parse_xml
from reading.models import Book, BookFile, ReadingImportRun, ReadingPosition
from reading.services import process_file, retry_file
from reading.storage import storage


def epub_bytes(body=None, extra=None):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("META-INF/container.xml", '<container><rootfiles><rootfile full-path="book.opf"/></rootfiles></container>')
        archive.writestr("book.opf", '<package xmlns="http://www.idpf.org/2007/opf" version="3.0"><metadata/><manifest><item id="c" href="chapter.xhtml" media-type="application/xhtml+xml"/></manifest><spine><itemref idref="c"/></spine></package>')
        archive.writestr("chapter.xhtml", body or '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>第一章</title></head><body><h1>第一章</h1><p>阅读测试文本。</p></body></html>')
        for name, data in (extra or {}).items():
            archive.writestr(name, data)
    return stream.getvalue()


class ReadingTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        settings = override_settings(KNOWLEDGE_FILE_ROOT=Path(self.tmp.name))
        settings.enable()
        self.addCleanup(settings.disable)
        self.family = Family.objects.create(name="阅读测试家庭")
        self.owner = self.member("reader-a", self.family)
        self.peer = self.member("reader-b", self.family)
        self.other = self.member("reader-c", Family.objects.create(name="另一个家庭"))
        self.client.force_login(self.owner.user)

    def member(self, name, family, role="member"):
        user = get_user_model().objects.create_user(username=name, password="test-only")
        return FamilyMember.objects.create(family=family, user=user, display_name=name, role=role)

    def upload(self, name="sample.epub", data=None, visibility=Book.PRIVATE):
        response = self.client.post(reverse("reading:upload"), {"title": "测试图书", "author": "作者",
            "visibility": visibility, "file": SimpleUploadedFile(name, data if data is not None else epub_bytes())})
        self.assertEqual(response.status_code, 302)
        return Book.objects.get(pk=response.url.strip('/').split('/')[-1])

    def url(self, name, book):
        return reverse("reading:"+name, args=[book.pk])

    def payload(self, book, revision=0, cfi="epubcfi(/6/2!/4/2:0)"):
        book.refresh_from_db()
        return {"file_hash": book.file.sha256, "normalizer_version": book.file.normalizer_version,
                "revision": revision, "location": {"cfi": cfi}, "progress": 4500}

    def test_empty_shelf_and_get_do_not_create_books_or_positions(self):
        response = self.client.get(reverse("reading:index"))
        self.assertContains(response, "从第一本书开始")
        self.assertEqual(Book.objects.count(), 0)
        self.assertEqual(ReadingPosition.objects.count(), 0)

    def test_upload_process_and_immutable_duplicate(self):
        book = self.upload()
        self.assertEqual(book.file.status, "queued")
        original = Path(storage().path(book.file.original_path)).read_bytes()
        call_command("process_reading_imports", limit=3, verbosity=0)
        book.refresh_from_db()
        self.assertEqual(book.file.status, "ready")
        self.assertEqual(book.file.text_status, "available")
        self.assertEqual(Path(storage().path(book.file.original_path)).read_bytes(), original)
        self.assertEqual(self.upload().pk, book.pk)
        self.assertEqual(Book.objects.count(), 1)
        self.assertFalse(process_file(book.file.pk))
        self.assertEqual(ReadingImportRun.objects.count(), 1)
        self.assertContains(self.client.get(self.url("detail", book)), "开始阅读")

    def test_private_book_all_endpoints_hidden_even_for_family_admin(self):
        book = self.upload()
        process_file(book.file.pk)
        self.peer.role = "admin"
        self.peer.save()
        for member in [self.peer, self.other]:
            self.client.force_login(member.user)
            for name in ["detail", "reader", "manifest", "resource", "file", "position", "edit"]:
                with self.subTest(member=member.pk, endpoint=name):
                    self.assertEqual(self.client.get(self.url(name, book)).status_code, 404)
            self.assertNotContains(self.client.get(reverse("reading:index")), "测试图书")

    def test_shared_book_has_private_position_and_revoke_hides_resources(self):
        book = self.upload(visibility=Book.FAMILY)
        process_file(book.file.pk)
        self.assertEqual(self.client.post(self.url("position", book), self.payload(book), content_type="application/json").status_code, 200)
        self.client.force_login(self.peer.user)
        self.assertEqual(self.client.get(self.url("position", book)).json()["revision"], 0)
        self.assertNotContains(self.client.get(self.url("detail", book)), "45%")
        self.assertEqual(self.client.get(self.url("manifest", book)).status_code, 200)
        self.assertEqual(self.client.post(self.url("edit", book), {"title":"修改"}).status_code, 404)
        Book.objects.filter(pk=book.pk).update(visibility=Book.PRIVATE)
        for name in ["manifest", "file", "position", "resource"]:
            self.assertEqual(self.client.get(self.url(name, book)).status_code, 404)

    def test_two_clients_conflict_and_explicit_retry(self):
        book = self.upload()
        process_file(book.file.pk)
        second = Client()
        second.force_login(self.owner.user)
        payload = self.payload(book)
        first = self.client.post(self.url("position", book), payload, content_type="application/json")
        self.assertEqual(first.json()["revision"], 1)
        conflict = second.post(self.url("position", book), payload, content_type="application/json")
        self.assertEqual(conflict.status_code, 409)
        payload["revision"] = conflict.json()["revision"]
        payload["progress"] = 1200
        accepted = second.post(self.url("position", book), payload, content_type="application/json")
        self.assertEqual(accepted.json()["revision"], 2)
        self.assertEqual(accepted.json()["progress"], 1200)
        self.assertEqual(ReadingPosition.objects.count(), 1)

    def test_progress_does_not_mark_complete_and_completion_preserves_location(self):
        book = self.upload()
        process_file(book.file.pk)
        payload = self.payload(book)
        payload["progress"] = 10000
        self.client.post(self.url("position", book), payload, content_type="application/json")
        self.assertIsNone(ReadingPosition.objects.get().completed_at)
        self.client.post(self.url("completion", book), {"completed":"yes"})
        position = ReadingPosition.objects.get()
        self.assertIsNotNone(position.completed_at)
        self.assertEqual(position.location, payload["location"])
        payload["revision"] = position.revision
        self.client.post(self.url("position", book), payload, content_type="application/json")
        self.assertIsNotNone(ReadingPosition.objects.get().completed_at)

    def test_malformed_location_and_changed_hash_rejected(self):
        book = self.upload()
        process_file(book.file.pk)
        for patch in [{"progress":-1},{"progress":True},{"revision":None},{"location":{}},{"file_hash":"other"}]:
            payload = self.payload(book)
            payload.update(patch)
            self.assertEqual(self.client.post(self.url("position", book), payload, content_type="application/json").status_code, 400)
        self.assertFalse(ReadingPosition.objects.exists())

    def test_csrf_and_read_only_role(self):
        book = self.upload(visibility=Book.FAMILY)
        process_file(book.file.pk)
        protected = Client(enforce_csrf_checks=True)
        protected.force_login(self.owner.user)
        self.assertEqual(protected.post(self.url("position", book), self.payload(book), content_type="application/json").status_code, 403)
        viewer = self.member("viewer", self.family, "viewer")
        self.client.force_login(viewer.user)
        self.assertEqual(self.client.get(self.url("reader", book)).status_code, 200)
        self.assertEqual(self.client.post(self.url("position", book), self.payload(book), content_type="application/json").status_code, 403)

    def test_range_suffix_invalid_head_if_range_and_cache(self):
        data = b"%PDF-1.7\n" + b"0123456789"*30 + b"\n%%EOF"
        book = self.upload("sample.pdf", data)
        url = self.url("file", book)
        response = self.client.get(url, HTTP_RANGE="bytes=2-9")
        self.assertEqual(response.status_code, 206)
        self.assertEqual(b"".join(response.streaming_content), data[2:10])
        self.assertEqual(response["Content-Range"], f"bytes 2-9/{len(data)}")
        self.assertEqual(response["Cache-Control"], "private, no-store")
        response = self.client.get(url, HTTP_RANGE="bytes=-5")
        self.assertEqual(b"".join(response.streaming_content), data[-5:])
        for value in ["bytes=-0", "bytes=1000-", "bytes=4-2", "bytes=0-1,4-5", "bad"]:
            self.assertEqual(self.client.get(url, HTTP_RANGE=value).status_code, 416)
        response = self.client.head(url, HTTP_RANGE="bytes=0-4")
        self.assertEqual(response["Content-Length"], "5")
        self.assertEqual(response.content, b"")
        response = self.client.get(url, HTTP_RANGE="bytes=0-4", HTTP_IF_RANGE='"old"')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(response.streaming_content), data)

    def test_resource_not_original_script_or_traversal(self):
        book = self.upload(data=epub_bytes('<html><head><script src="/accounts/logout/">bad()</script></head><body onload="bad()"><p style="background:url(https://example.com)">正文</p><iframe src="/admin/"/><a href="../../admin/">危险链接</a><a href="https://example.com">外链</a></body></html>'))
        process_file(book.file.pk)
        response = self.client.get(self.url("resource", book), {"path":"chapter.xhtml"})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"script", response.content)
        self.assertNotIn(b"onload", response.content)
        self.assertNotIn(b"https://", response.content)
        self.assertNotIn(b"/admin/", response.content)
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertIn("sandbox", response["Content-Security-Policy"])
        self.assertEqual(self.client.get(self.url("resource", book), {"path":"../original.epub"}).status_code,404)

    def test_bad_archive_fails_with_auditable_retry(self):
        book = self.upload(data=epub_bytes(extra={"../escape":"x"}))
        with self.assertRaises(CommandError):
            call_command("process_reading_imports")
        book.refresh_from_db()
        self.assertEqual(book.file.status, "failed")
        self.assertEqual(book.file.runs.count(), 1)
        self.assertEqual(self.client.post(self.url("retry", book)).status_code, 302)
        book.refresh_from_db()
        self.assertEqual(book.file.status, "queued")

    def test_stale_job_retry_and_ready_derivative_cannot_be_replaced(self):
        book = self.upload()
        BookFile.objects.filter(book=book).update(status="processing", processing_started_at=timezone.now()-timedelta(minutes=16))
        retry_file(book)
        process_file(book.file.pk)
        book.refresh_from_db()
        self.assertEqual(book.file.status,"ready")
        self.client.post(self.url("retry",book))
        self.assertEqual(BookFile.objects.get(book=book).status,"ready")

    def test_txt_and_mobi_capabilities(self):
        book=self.upload("test.txt", "第一段\n第二段<script>内容</script>".encode())
        self.assertTrue(process_file(book.file.pk))
        book.refresh_from_db()
        self.assertEqual(book.file.text_status,"available")
        response=self.client.get(self.url("resource",book),{"path":"part-0.xhtml"})
        self.assertIn(b"&lt;script&gt;",response.content)
        mobi=self.upload("test.mobi",b"unparsed mobi sample")
        self.assertFalse(process_file(mobi.file.pk))
        mobi.refresh_from_db()
        self.assertEqual(mobi.file.status,"unsupported")

    @override_settings(READING_MAX_UPLOAD_BYTES=5)
    def test_upload_size_limit(self):
        response=self.client.post(reverse("reading:upload"),{"title":"大文件","visibility":"private","file":SimpleUploadedFile("a.epub",b"123456")})
        self.assertEqual(response.status_code,200)
        self.assertFalse(Book.objects.exists())

    def test_entity_and_utf16_xml_fail_closed(self):
        for data in [b'<!DOCTYPE x [<!ENTITY a "boom">]><html>&a;</html>', '<html>text</html>'.encode('utf-16')]:
            with self.assertRaises(ImportFailure):
                parse_xml(data)
