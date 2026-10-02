import copy
import base64
import json
from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from threading import Barrier
from unittest import skipUnless
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.core.management import call_command, CommandError
from django.db import connection, connections
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.urls import reverse

from family_core.models import Family, FamilyMember
from ai_analysis.models import AiProvider, AiAnalysisRequest
from .ai import KnowledgeAiError
from .models import KnowledgeAsset, KnowledgeDocument, KnowledgeJob, KnowledgeRevision, KnowledgeWebCapture, KnowledgeProposal, KnowledgeProposalRun
from .permissions import accessible_documents, accessible_search_entries
from .services import claim_next_job, process_job, rebuild_document_normalized_content
from .web_capture import firecrawl_scrape, normalize_web_html, queue_capture, submit_capture
from .web_fetch import WebCaptureError, canonical_url, public_addresses, public_request

SNAPSHOT = {"url": "https://example.com/article", "metadata": {"title": "家庭的长期知识", "author": "作者甲"},
    "html": '<h1>家庭的长期知识</h1><p>正文观点：长期保存资料。</p><img src="/chart.png" alt="静态图表"><a href="/related">相关文章</a>',
    "rawHtml": "原始网页", "markdown": "# 家庭的长期知识\n正文观点：长期保存资料。"}
PNG = b"\x89PNG\r\n\x1a\n" + b"example-image"


class WebFetchTests(SimpleTestCase):
    def test_url_canonicalization_preserves_query_drops_fragment(self):
        self.assertEqual(canonical_url("HTTPS://Example.COM:443/article?a=1#here"), "https://example.com/article?a=1")
        self.assertEqual(canonical_url("https://[2606:4700:4700::1111]/"), "https://[2606:4700:4700::1111]/")

    def test_private_and_credential_urls_rejected(self):
        for url in ["file:///etc/passwd", "http://localhost/", "http://127.0.0.1/", "http://10.0.0.1/", "https://user:pass@example.com/", "http://nas.local/", "https://example.com:8000/", "https://example.com/\nsecret", "http://169.254.169.254/latest/", "http://[::1]/", "http://224.0.0.1/"]:
            with self.subTest(url=url), self.assertRaises(WebCaptureError):
                canonical_url(url)

    @patch("knowledge.web_fetch.socket.getaddrinfo")
    def test_mixed_public_private_dns_rejected(self, dns):
        dns.return_value = [(2, 1, 6, "", ("1.1.1.1", 443)), (2, 1, 6, "", ("192.168.1.2", 443))]
        with self.assertRaises(WebCaptureError):
            public_addresses("https://example.com/")

    @patch("knowledge.web_fetch.PinnedHTTPSConnection")
    @patch("knowledge.web_fetch.public_addresses", return_value=["1.1.1.1"])
    def test_redirect_to_private_network_blocked(self, dns, connection):
        response = connection.return_value.getresponse.return_value
        response.status = 302
        response.getheader.return_value = "http://127.0.0.1/private"
        with self.assertRaises(WebCaptureError):
            public_request("https://example.com/", limit=100)
        self.assertEqual(connection.call_count, 1)

    @patch("knowledge.web_fetch.PinnedHTTPSConnection")
    @patch("knowledge.web_fetch.public_addresses", return_value=["1.1.1.1"])
    def test_response_size_bound(self, dns, connection):
        response = connection.return_value.getresponse.return_value
        response.status = 200
        response.getheader.side_effect = lambda key, default=None: default
        response.read1.return_value = b"x" * 101
        with self.assertRaises(WebCaptureError):
            public_request("https://example.com/", limit=100)
        connection.assert_called_once_with("example.com", "1.1.1.1", connection.call_args.args[2])

    def test_html_sanitization_and_missing_image_marker(self):
        snapshot = copy.deepcopy(SNAPSHOT)
        snapshot["html"] += '<script>secret()</script><iframe src="http://internal/"></iframe><img src="http://internal/x" onerror="bad()"><a href="javascript:bad()">坏链接</a>'
        safe, text = normalize_web_html(snapshot, {"https://example.com/chart.png": "/knowledge/assets/1/download/"})
        self.assertIn('/knowledge/assets/1/download/', safe)
        self.assertIn('https://example.com/related', safe)
        self.assertIn("图片未保存", text)
        for forbidden in ["secret()", "iframe", "onerror", "javascript:", 'src="http']:
            self.assertNotIn(forbidden, safe)

    def test_missing_firecrawl_key_is_actionable(self):
        with override_settings(KNOWLEDGE_FIRECRAWL_API_KEY=""), self.assertRaisesMessage(WebCaptureError, "KNOWLEDGE_FIRECRAWL_API_KEY"):
            firecrawl_scrape("https://example.com/")

    @override_settings(KNOWLEDGE_FIRECRAWL_API_KEY="test-only")
    @patch("knowledge.web_capture.public_addresses", return_value=["1.1.1.1"])
    @patch("knowledge.web_capture.public_request")
    def test_firecrawl_request_and_status_handling(self, request, dns):
        request.return_value = (json.dumps({"success": True, "data": {"html": "<p>正文</p>", "metadata": {"statusCode": 200}}}).encode(), "application/json")
        self.assertEqual(firecrawl_scrape("https://example.com/")["html"], "<p>正文</p>")
        payload = json.loads(request.call_args.kwargs["data"])
        self.assertEqual(payload["formats"], ["markdown", "html", "rawHtml"])
        self.assertEqual(request.call_args.kwargs["redirects"], 0)
        request.return_value = (json.dumps({"success": True, "data": {"html": "<p>登录</p>", "metadata": {"statusCode": 403}}}).encode(), "application/json")
        with self.assertRaises(WebCaptureError):
            firecrawl_scrape("https://example.com/")


class WebCaptureTests(TestCase):
    def setUp(self):
        self.family = Family.objects.create(name="收藏测试家庭")
        self.user = get_user_model().objects.create_user(username="collector", password="test-only")
        self.member = FamilyMember.objects.create(family=self.family, user=self.user, display_name="收藏者")
        self.other_user = get_user_model().objects.create_user(username="other", password="test-only", is_staff=True, is_superuser=True)
        self.other = FamilyMember.objects.create(family=self.family, user=self.other_user, display_name="管理员", role=FamilyMember.ROLE_ADMIN)
        self.client.force_login(self.user)

    def submit(self, **kwargs):
        values = {"url": SNAPSHOT["url"], "visibility": "private", "organize_with_ai": False, **kwargs}
        return submit_capture(self.member, **values)[0]

    def run_capture(self, snapshot=None, image=(PNG, "image/png"), ai=None):
        with patch("knowledge.web_capture.firecrawl_scrape", return_value=copy.deepcopy(snapshot or SNAPSHOT)) as scrape, patch("knowledge.web_capture.public_request", return_value=image) as images, patch("knowledge.web_capture.generate_proposals", side_effect=ai) as proposals:
            job = claim_next_job()
            process_job(job)
        return job, scrape, images, proposals

    def test_submit_is_atomic_and_duplicate_preserves_privacy(self):
        capture = self.submit(note="留存原文")
        duplicate, created = submit_capture(self.member, url=SNAPSHOT["url"]+"#part", visibility="family", organize_with_ai=True)
        self.assertFalse(created)
        self.assertEqual(capture.pk, duplicate.pk)
        self.assertEqual(duplicate.visibility, "private")
        self.assertEqual(KnowledgeJob.objects.count(), 1)
        self.assertEqual(KnowledgeDocument.objects.count(), 0)

    def test_full_archive_safe_body_and_protected_image(self):
        capture = self.submit()
        job, _, images, ai = self.run_capture()
        capture.refresh_from_db()
        self.assertEqual(job.status, "success")
        self.assertEqual(capture.document.knowledge_status, "pending")
        self.assertEqual(capture.document.library_tier, "archive")
        ai.assert_not_called()
        images.assert_called_once()
        revision = capture.document.current_revision
        self.assertEqual(revision.assets.count(), 1)
        self.assertIn("/knowledge/assets/", revision.normalized_html)
        self.assertNotIn('src="https:', revision.normalized_html)
        with revision.raw_file.open() as raw:
            self.assertEqual(json.load(raw), SNAPSHOT)
        raw_response = self.client.get(reverse("knowledge:revision_raw_download", args=[revision.pk]))
        self.assertIn(".json", raw_response["Content-Disposition"])
        self.assertEqual(json.loads(b"".join(raw_response.streaming_content)), SNAPSHOT)
        self.assertFalse(rebuild_document_normalized_content(capture.document)["changed"])
        self.assertContains(self.client.get(reverse("knowledge:document_detail", args=[capture.document_id])), "网页归档快照")

    def test_ai_failure_keeps_body_and_retry_does_not_recrawl(self):
        capture = self.submit(organize_with_ai=True)
        job, _, _, ai = self.run_capture(ai=KnowledgeAiError("AI 暂不可用"))
        capture.refresh_from_db()
        self.assertEqual(job.status, "failed")
        self.assertIsNotNone(capture.document_id)
        self.assertIn("AI", capture.error_message)
        queue_capture(capture)
        job, scrape, images, ai = self.run_capture()
        self.assertEqual(job.status, "success")
        scrape.assert_not_called()
        images.assert_not_called()
        ai.assert_called_once()
        self.assertEqual(ai.call_args.kwargs["cloud_ai_consent"], "one_time")

    def test_image_failure_and_image_only_retry(self):
        capture = self.submit(organize_with_ai=True)
        with patch("knowledge.web_capture.public_request", side_effect=WebCaptureError("图床不可访问")), patch("knowledge.web_capture.firecrawl_scrape", return_value=copy.deepcopy(SNAPSHOT)), patch("knowledge.web_capture.generate_proposals"):
            job = claim_next_job()
            process_job(job)
        capture.refresh_from_db()
        self.assertEqual(job.status, "partial")
        self.assertEqual(len(capture.image_failures), 1)
        self.assertIn("图片未保存", capture.document.current_revision.normalized_html)
        queue_capture(capture, "images")
        job, scrape, images, ai = self.run_capture()
        capture.refresh_from_db()
        self.assertEqual(job.status, "success")
        self.assertEqual(capture.image_failures, [])
        scrape.assert_not_called()
        ai.assert_not_called()
        self.assertNotIn("图片未保存", capture.document.current_revision.normalized_html)

    def test_embedded_bitmap_is_archived_without_network_or_data_uri_display(self):
        capture = self.submit()
        snapshot = copy.deepcopy(SNAPSHOT)
        snapshot["html"] = '<p>静态图表</p><img alt="内嵌图表" src="data:image/png;base64,' + base64.b64encode(PNG).decode() + '">'
        job, _, images, _ = self.run_capture(snapshot=snapshot)
        capture.refresh_from_db()
        self.assertEqual(job.status, "success")
        images.assert_not_called()
        revision = capture.document.current_revision
        self.assertEqual(revision.assets.count(), 1)
        self.assertIn("/knowledge/assets/", revision.normalized_html)
        self.assertNotIn("data:image", revision.normalized_html)
        self.assertFalse(rebuild_document_normalized_content(capture.document)["changed"])

    def test_recapture_preserves_versions_and_reuses_identical_snapshot(self):
        capture = self.submit()
        self.run_capture()
        capture.refresh_from_db()
        queue_capture(capture, "recapture")
        self.run_capture()
        self.assertEqual(KnowledgeRevision.objects.count(), 1)
        snapshot = copy.deepcopy(SNAPSHOT)
        snapshot["html"] += "<p>新观点。</p>"
        queue_capture(capture, "recapture")
        self.run_capture(snapshot=snapshot)
        self.assertEqual(KnowledgeRevision.objects.count(), 2)
        self.assertEqual(KnowledgeAsset.objects.count(), 2)

    def test_cancel_during_fetch_does_not_save(self):
        capture = self.submit()
        job = claim_next_job()
        def cancelled(url):
            KnowledgeJob.objects.filter(pk=job.pk).update(status="cancel_requested")
            return copy.deepcopy(SNAPSHOT)
        with patch("knowledge.web_capture.firecrawl_scrape", side_effect=cancelled):
            process_job(job)
        self.assertEqual(job.status, "cancelled")
        self.assertEqual(KnowledgeDocument.objects.count(), 0)

    def test_return_to_old_snapshot_reuses_version_but_regenerates_stale_ai(self):
        capture = self.submit(organize_with_ai=True)
        self.run_capture()
        capture.refresh_from_db()
        original_revision = capture.document.current_revision
        KnowledgeProposalRun.objects.create(document=capture.document, revision=original_revision, sequence=1, requested_by=self.member,
            model_name="test-only", prompt_version="test-only", content_hash=original_revision.content_hash)
        changed = copy.deepcopy(SNAPSHOT)
        changed["html"] += "<p>不同正文。</p>"
        queue_capture(capture, "recapture")
        self.run_capture(snapshot=changed)
        queue_capture(capture, "recapture")
        job, _, _, ai = self.run_capture()
        capture.refresh_from_db()
        self.assertEqual(KnowledgeRevision.objects.count(), 2)
        self.assertEqual(capture.document.current_revision_id, original_revision.pk)
        ai.assert_called_once()

    def test_superseded_worker_cannot_overwrite_new_job(self):
        capture = self.submit()
        old = claim_next_job()
        KnowledgeJob.objects.filter(pk=old.pk).update(status="failed")
        new, _ = queue_capture(capture)
        with patch("knowledge.web_capture.firecrawl_scrape") as scrape:
            process_job(old)
        scrape.assert_not_called()
        new.refresh_from_db()
        self.assertEqual(new.status, "pending")

    def test_private_records_denied_even_to_family_superuser(self):
        capture = self.submit()
        self.run_capture()
        capture.refresh_from_db()
        asset = capture.document.current_revision.assets.first()
        self.client.force_login(self.other_user)
        for name, pk in [("web_capture_detail", capture.pk), ("job_detail", capture.last_job_id), ("document_detail", capture.document_id), ("asset_download", asset.pk)]:
            self.assertEqual(self.client.get(reverse("knowledge:"+name, args=[pk])).status_code, 404)
        self.assertEqual(accessible_documents(self.other).count(), 0)
        self.assertEqual(accessible_search_entries(self.other).count(), 0)
        self.assertNotContains(self.client.get(reverse("knowledge:web_captures")), SNAPSHOT["url"])

    def test_shared_body_visible_but_owner_control_record_private(self):
        capture = self.submit(visibility="family", note="我的备注")
        self.run_capture()
        capture.refresh_from_db()
        self.client.force_login(self.other_user)
        self.assertEqual(self.client.get(reverse("knowledge:document_detail", args=[capture.document_id])).status_code, 200)
        self.assertEqual(self.client.get(reverse("knowledge:web_capture_detail", args=[capture.pk])).status_code, 404)

    def test_web_source_directory_filter_is_not_other_sources(self):
        capture = self.submit()
        self.run_capture()
        capture.refresh_from_db()
        response = self.client.get(reverse("knowledge:library"), {"collection": "archive", "directory": "source", "source_group": "web_capture"})
        self.assertEqual(response.context["selected_source_group"], "web_capture")
        self.assertEqual(response.context["page_obj"].paginator.count, 1)
        groups = response.context["source_directory"]
        self.assertEqual([g["id"] for g in groups], ["web_capture"])
        self.assertEqual(self.client.get(reverse("knowledge:library"), {"collection": "archive", "source_group": "other"}).context["page_obj"].paginator.count, 0)

    def test_get_is_read_only_and_post_requires_consent(self):
        with patch("knowledge.web_capture.firecrawl_scrape") as scrape:
            self.assertEqual(self.client.get(reverse("knowledge:web_captures")).status_code, 200)
            response = self.client.post(reverse("knowledge:web_captures"), {"url": SNAPSHOT["url"], "visibility": "private"})
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "这个字段是必填项")
            scrape.assert_not_called()
        self.assertEqual(KnowledgeWebCapture.objects.count(), 0)

    def test_stale_failed_task_has_visible_retry_even_without_capture_error(self):
        capture = self.submit()
        KnowledgeJob.objects.filter(pk=capture.last_job_id).update(status="failed", error_message="任务心跳超过 30 分钟")
        response = self.client.get(reverse("knowledge:web_capture_detail", args=[capture.pk]))
        self.assertContains(response, "继续保存 / 重试失败步骤")
        self.assertContains(response, "任务心跳超过 30 分钟")

    def test_active_capture_cannot_queue_second_ai_task(self):
        capture = self.submit()
        self.run_capture()
        capture.refresh_from_db()
        queue_capture(capture, "images")
        response = self.client.post(reverse("knowledge:document_ai_organize"), {"document_ids": [capture.document_id]})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(KnowledgeJob.objects.filter(job_type="generate_proposals").count(), 0)

    def test_failed_snapshot_write_removes_its_new_file(self):
        capture = self.submit()
        original = KnowledgeRevision.save
        storage = KnowledgeRevision._meta.get_field("raw_file").storage
        def fail_raw_save(revision, *args, **kwargs):
            if kwargs.get("update_fields") == ["raw_file"]:
                raise OSError("test-only storage/db failure")
            return original(revision, *args, **kwargs)
        with patch.object(KnowledgeRevision, "save", autospec=True, side_effect=fail_raw_save), patch.object(storage, "delete", wraps=storage.delete) as delete:
            job, _, _, _ = self.run_capture()
        self.assertEqual(job.status, "failed")
        self.assertEqual(KnowledgeDocument.objects.count(), 0)
        self.assertEqual(KnowledgeRevision.objects.count(), 0)
        delete.assert_called_once()

    def test_viewer_cannot_submit_or_retry(self):
        capture = self.submit()
        self.member.role = FamilyMember.ROLE_VIEWER
        self.member.save(update_fields=["role"])
        self.assertEqual(self.client.post(reverse("knowledge:web_captures"), {}).status_code, 403)
        self.assertEqual(self.client.post(reverse("knowledge:web_capture_retry", args=[capture.pk])).status_code, 403)

    def test_command_failed_capture_returns_nonzero(self):
        self.submit()
        with patch("knowledge.web_capture.firecrawl_scrape", side_effect=WebCaptureError("抓取失败")), self.assertRaises(CommandError):
            call_command("process_knowledge_jobs", stdout=StringIO())

    def test_long_body_archived_without_silent_ai_truncation(self):
        capture = self.submit(organize_with_ai=True)
        snapshot = copy.deepcopy(SNAPSHOT)
        snapshot["html"] = "<p>" + "资料" * 41000 + "</p>"
        job, _, _, ai = self.run_capture(snapshot=snapshot)
        capture.refresh_from_db()
        self.assertEqual(job.status, "failed")
        self.assertGreater(len(capture.document.current_revision.plain_text), 80000)
        ai.assert_not_called()

    def test_actual_ai_pipeline_only_saves_pending_proposals_and_audit(self):
        capture = self.submit(organize_with_ai=True)
        AiProvider.objects.create(name="离线文本测试", provider_type="openai_compatible", base_url="https://api.example.com/v1", model_name="test-text", extra_data={"api_key_env_var": "TEST_KNOWLEDGE_AI_KEY"})
        payload = {"choices": [{"message": {"content": json.dumps({"summary": "长期保存资料。", "category": "学习", "tags": ["知识"]})}}], "usage": {"total_tokens": 10}}
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = json.dumps(payload).encode()
        with patch.dict("os.environ", {"TEST_KNOWLEDGE_AI_KEY": "test-only"}), patch("knowledge.ai.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("1.1.1.1", 443))]), patch("knowledge.ai.urllib.request.urlopen", return_value=response), patch("knowledge.web_capture.firecrawl_scrape", return_value=copy.deepcopy(SNAPSHOT)), patch("knowledge.web_capture.public_request", return_value=(PNG, "image/png")):
            job = claim_next_job()
            process_job(job)
        capture.refresh_from_db()
        self.assertEqual(job.status, "success")
        self.assertEqual(capture.document.curation_status, "pending_review")
        self.assertEqual(capture.document.library_tier, "archive")
        self.assertEqual(capture.document.confirmed_summary, "")
        self.assertEqual(KnowledgeProposal.objects.filter(status="pending").count(), 3)
        request = AiAnalysisRequest.objects.get(module="knowledge")
        self.assertEqual(request.scope["cloud_ai_consent"], "one_time")
        self.assertFalse(capture.document.source.allow_cloud_ai)
        queue_capture(capture)
        job, scrape, images, ai = self.run_capture()
        ai.assert_not_called()
        scrape.assert_not_called()

    def test_cancel_during_actual_ai_preserves_body_but_discards_proposals(self):
        capture = self.submit(organize_with_ai=True)
        AiProvider.objects.create(name="离线文本测试", provider_type="openai_compatible", base_url="https://api.example.com/v1", model_name="test-text", extra_data={"api_key_env_var": "TEST_KNOWLEDGE_AI_KEY"})
        job = claim_next_job()
        payload = {"choices": [{"message": {"content": json.dumps({"summary": "摘要", "category": "学习", "tags": []})}}]}
        def cancel_and_return(*args):
            KnowledgeJob.objects.filter(pk=job.pk).update(status="cancel_requested")
            return json.dumps(payload).encode()
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.side_effect = cancel_and_return
        with patch.dict("os.environ", {"TEST_KNOWLEDGE_AI_KEY": "test-only"}), patch("knowledge.ai.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("1.1.1.1", 443))]), patch("knowledge.ai.urllib.request.urlopen", return_value=response), patch("knowledge.web_capture.firecrawl_scrape", return_value=copy.deepcopy(SNAPSHOT)), patch("knowledge.web_capture.public_request", return_value=(PNG, "image/png")):
            process_job(job)
        capture.refresh_from_db()
        self.assertEqual(job.status, "cancelled")
        self.assertIsNotNone(capture.document_id)
        self.assertEqual(KnowledgeProposal.objects.count(), 0)
        self.assertEqual(AiAnalysisRequest.objects.get(module="knowledge").status, "failed")


@skipUnless(connection.vendor == "postgresql", "Row-lock checks require isolated PostgreSQL")
class WebCaptureConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.family = Family.objects.create(name="并发测试家庭")
        self.member = FamilyMember.objects.create(family=self.family, display_name="收藏成员")

    def concurrent(self, callback):
        barrier = Barrier(2)
        def task():
            connections.close_all()
            try:
                member = FamilyMember.objects.get(pk=self.member.pk)
                barrier.wait(timeout=5)
                return callback(member)
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as executor:
            return list(executor.map(lambda _: task(), range(2)))

    def test_concurrent_first_submission_has_one_capture_and_one_job(self):
        results = self.concurrent(lambda member: submit_capture(member, url=SNAPSHOT["url"], visibility="private", organize_with_ai=False)[0].pk)
        self.assertEqual(results[0], results[1])
        self.assertEqual(KnowledgeWebCapture.objects.count(), 1)
        self.assertEqual(KnowledgeJob.objects.count(), 1)

    def test_concurrent_retry_has_one_active_job(self):
        capture = submit_capture(self.member, url=SNAPSHOT["url"], visibility="private", organize_with_ai=False)[0]
        KnowledgeJob.objects.filter(pk=capture.last_job_id).update(status="failed")
        results = self.concurrent(lambda member: queue_capture(KnowledgeWebCapture.objects.get(pk=capture.pk))[0].pk)
        self.assertEqual(results[0], results[1])
        self.assertEqual(KnowledgeJob.objects.filter(status="pending").count(), 1)
        self.assertEqual(KnowledgeJob.objects.count(), 2)
