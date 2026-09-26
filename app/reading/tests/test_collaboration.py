import json
from unittest.mock import patch
from django.test import TestCase
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.core.files.uploadedfile import SimpleUploadedFile
from ai_analysis.models import AiProvider, AiAnalysisRequest
from knowledge.permissions import accessible_documents, accessible_search_entries
from reading import ai
from reading.artifacts import validate_data, export_html, publish, archive_version
from reading.models import Annotation, AnnotationComment, Book, ReadingArtifactVersion, ReadingPlan, ReadingPlanItem, ReadingAiJob
from reading.services import process_file
from .test_reading import ReadingTests


def structured():
    return {"schema_version":1,"title":"阅读成果测试","sources":[{"id":"b1","quote":"外部文字","verified":True}],
        "sections":[{"id":"s1","title":"观点","kind":"book","content":"测试观点","source_ids":["b1"]}],
        "nodes":[{"id":"n1","title":"概念","parent":"","section_id":"s1","source_ids":["b1"]}]}


class CollaborationTests(TestCase):
    setUp=ReadingTests.setUp
    member=ReadingTests.member
    upload=ReadingTests.upload
    url=ReadingTests.url
    payload=ReadingTests.payload

    def ready(self):
        book=self.upload(visibility=Book.FAMILY);process_file(book.file.pk);book.refresh_from_db();return book

    def annotation(self,book,visibility="private"):
        payload={**self.payload(book),"quote":"阅读测试文本。","note":"我的观点","visibility":visibility,
                 "anchor":{"cfi":"epubcfi(/6/2!/4/4:0)","section":0}}
        response=self.client.post(self.url("annotations",book),payload,content_type="application/json")
        self.assertEqual(response.status_code,201,response.content)
        return Annotation.objects.get(pk=response.json()["id"])

    def test_notes_explicit_share_comments_author_edit_and_revocation(self):
        book=self.ready();note=self.annotation(book)
        url=reverse("reading:note",args=[note.pk]);comments=reverse("reading:comment",args=[note.pk])
        self.client.force_login(self.peer.user)
        self.assertEqual(self.client.get(url).status_code,404)
        self.assertEqual(self.client.post(comments,{"body":"窥探"}).status_code,404)
        self.assertEqual(self.client.get(self.url("annotations",book)).json()["items"],[])
        self.client.force_login(self.owner.user)
        self.client.post(url,{"revision":1,"note":"已分享","visibility":"family"})
        self.client.force_login(self.peer.user)
        self.assertContains(self.client.get(url),"已分享")
        self.assertEqual(self.client.post(url,{"revision":2,"note":"篡改","visibility":"family"}).status_code,404)
        self.client.post(comments,{"body":"讨论内容"});comment=AnnotationComment.objects.get()
        edit=reverse("reading:comment_edit",args=[comment.pk])
        self.client.post(edit,{"body":"自己的修改","revision":1})
        self.client.post(edit,{"body":"旧版本覆盖","revision":1})
        comment.refresh_from_db();self.assertEqual(comment.body,"自己的修改")
        self.client.force_login(self.owner.user)
        self.assertEqual(self.client.post(edit,{"body":"他人修改","revision":2}).status_code,404)
        self.client.post(url,{"revision":2,"note":"撤回","visibility":"private"})
        self.client.force_login(self.peer.user)
        self.assertEqual(self.client.get(url).status_code,404)
        self.assertEqual(self.client.post(edit,{"body":"撤回后修改","revision":2}).status_code,404)

    def test_note_quote_matching_and_stale_edit(self):
        book=self.ready();note=self.annotation(book)
        self.assertTrue(note.text_matched)
        url=reverse("reading:note",args=[note.pk])
        self.client.post(url,{"revision":1,"note":"更新","visibility":"private"})
        self.client.post(url,{"revision":1,"note":"旧内容","visibility":"private"})
        note.refresh_from_db();self.assertEqual(note.note,"更新")
        bad={**self.payload(book),"quote":"外部文字","anchor":{"cfi":"epubcfi(/6/2!/4/4:0)","section":99}}
        self.assertEqual(self.client.post(self.url("annotations",book),bad,content_type="application/json").status_code,400)

    def test_plan_private_manual_completion_and_online_position_separate(self):
        book=self.ready()
        response=self.client.post(reverse("reading:plans"),{"title":"秋季阅读","target_date":"2026-12-31"})
        self.assertEqual(response.status_code,302);plan=ReadingPlan.objects.get()
        self.client.post(response.url,{"action":"settings","settings-title":"年度阅读","settings-target_date":"2027-01-31"})
        plan.refresh_from_db();self.assertEqual(plan.title,"年度阅读")
        self.client.post(response.url,{"action":"books","books":[str(book.pk)]})
        self.client.post(response.url,{"title":"纸书","kind":"paper"})
        item=plan.items.get(kind="paper")
        progress=reverse("reading:plan_progress",args=[item.pk])
        self.client.post(progress,{"progress":100,"revision":1})
        item.refresh_from_db();self.assertIsNone(item.completed_at)
        self.client.post(progress,{"progress":90,"revision":2,"done":"yes"})
        item.refresh_from_db();self.assertIsNotNone(item.completed_at)
        self.assertContains(self.client.get(response.url),"1 / 2")
        self.client.force_login(self.peer.user)
        self.assertEqual(self.client.get(response.url).status_code,404)
        self.assertEqual(self.client.post(progress,{"progress":10,"revision":3}).status_code,404)
        Book.objects.filter(pk=book.pk).update(visibility="private")
        ReadingPlanItem.objects.create(plan=ReadingPlan.objects.create(member=self.peer,title="同伴计划",target_date="2026-12-31"),book=book,title="秘密标题",kind="online")
        own=ReadingPlan.objects.get(member=self.peer)
        page=self.client.get(reverse("reading:plan",args=[own.pk]))
        self.assertNotContains(page,"秘密标题");self.assertContains(page,"图书已不可访问")

    def publication(self,book,visibility="family"):
        data=validate_data(structured())
        return publish(book,self.owner,data["title"],visibility,export_html(data),data)[0]

    def test_external_import_duplicate_and_preview_sandbox_direct_url(self):
        book=self.ready();url=self.url("artifact_upload",book)
        raw=b'<html><script>parent.document.body.textContent="bad"</script>Sample</html>'
        for i in range(2):
            response=self.client.post(url,{"title":"外部成果","visibility":"private","file":SimpleUploadedFile("result.html",raw)})
            self.assertEqual(response.status_code,302)
        self.assertEqual(ReadingArtifactVersion.objects.count(),1)
        version=ReadingArtifactVersion.objects.get()
        page=self.client.get(reverse("reading:artifact_file",args=[version.artifact_id,1]),{"preview":"1"})
        self.assertEqual(page.content,raw)
        csp=page["Content-Security-Policy"]
        self.assertIn("sandbox allow-scripts",csp);self.assertNotIn("allow-same-origin",csp);self.assertIn("connect-src 'none'",csp)
        self.assertContains(self.client.get(response.url),"对应关系尚未核验")
        self.client.force_login(self.peer.user)
        self.assertEqual(self.client.get(response.url).status_code,404)
        self.assertEqual(self.client.get(reverse("reading:artifact_file",args=[version.artifact_id,1])).status_code,404)

    def test_structured_source_cannot_assert_verified_or_inject_html(self):
        data=structured();data["sections"][0]["content"]='<script>alert(1)</script>'
        data=validate_data(data)
        self.assertFalse(data["sources"][0]["verified"])
        book=self.ready();version=publish(book,self.owner,"成果","private",export_html(data),data)[0]
        response=self.client.get(reverse("reading:artifact",args=[version.artifact_id]))
        self.assertContains(response,"&lt;script&gt;")
        self.assertNotContains(response,"<script>alert(1)</script>")

    def test_cycle_unknown_reference_and_deep_tree_rejected(self):
        data=structured();data["nodes"][0]["parent"]="n1"
        with self.assertRaises(ValidationError):validate_data(data)
        data=structured();data["sections"][0]["source_ids"]=["missing"]
        with self.assertRaises(ValidationError):validate_data(data)
        data=structured();data["nodes"][0]["section_id"]="absent"
        with self.assertRaises(ValidationError):validate_data(data)

    def test_versions_immutable_archive_idempotent_and_revoked_at_every_entry(self):
        book=self.ready();version=self.publication(book);artifact=version.artifact
        doc=archive_version(version,self.owner)
        self.assertEqual(archive_version(version,self.owner).pk,doc.pk)
        edit=reverse("reading:artifact_edit",args=[artifact.pk])
        data=dict(version.data);data["title"]="更新成果"
        response=self.client.post(edit,{"data":json.dumps(data),"base_version":version.pk})
        self.assertEqual(response.status_code,302)
        self.assertEqual(artifact.versions.count(),2)
        self.client.post(edit,{"data":json.dumps(data),"base_version":version.pk})
        self.assertEqual(artifact.versions.count(),2)
        doc.refresh_from_db();self.assertIn("阅读成果测试",doc.title)
        self.assertTrue(accessible_documents(self.peer).filter(pk=doc.pk).exists())
        self.assertTrue(accessible_search_entries(self.peer).filter(document=doc).exists())
        Book.objects.filter(pk=book.pk).update(visibility="private")
        self.assertFalse(accessible_documents(self.peer).filter(pk=doc.pk).exists())
        self.assertFalse(accessible_search_entries(self.peer).filter(document=doc).exists())
        self.client.force_login(self.peer.user)
        self.assertEqual(self.client.get(reverse("knowledge:document_detail",args=[doc.pk])).status_code,404)

    def job(self):
        book=self.ready();provider=AiProvider.objects.create(name="模拟服务商",provider_type="openai_compatible",model_name="test-model",base_url="https://example.com/v1")
        return ai.draft(book,self.owner,provider,{"kind":"chapter","section":0})

    def test_ai_never_sends_draft_then_confirm_claim_and_publish_once(self):
        job=self.job();response=structured()
        with patch("reading.ai.request_completion",return_value=(response,100)) as send:
            self.assertFalse(ai.process_job(job.pk));send.assert_not_called()
            ai.confirm(job);ai.confirm(job)
            ai.process_job(job.pk);ai.process_job(job.pk)
            self.assertEqual(send.call_count,1)
        job.refresh_from_db();self.assertEqual(job.status,"success")
        self.assertTrue(job.result["sources"][0]["verified"])
        self.assertIn("阅读测试文本",job.result["sources"][0]["quote"])
        self.assertEqual(ai.publish_job(job).pk,ai.publish_job(job).pk)
        self.assertEqual(AiAnalysisRequest.objects.count(),1)
        self.assertNotIn("阅读测试文本",str(AiAnalysisRequest.objects.get().sanitized_input))
        self.assertEqual(ai.publish_job(job).visibility,"private")

    def test_ai_model_change_and_permission_revoke_prevent_dispatch(self):
        job=self.job();ai.confirm(job)
        job.provider.model_name="different";job.provider.save()
        with patch("reading.ai.request_completion") as send:ai.process_job(job.pk);send.assert_not_called()
        job.refresh_from_db();self.assertEqual(job.status,"failed")
        self.assertIn("配置已经变化",job.error)

    def test_ai_cancel_running_discards_result_no_auto_retry(self):
        job=self.job();ai.confirm(job)
        def cancelled(_):
            ReadingAiJob.objects.filter(pk=job.pk).update(status="cancelled")
            return structured(),50
        with patch("reading.ai.request_completion",side_effect=cancelled):ai.process_job(job.pk)
        job.refresh_from_db();self.assertEqual(job.status,"cancelled");self.assertEqual(job.result,{})
        with self.assertRaises(ValidationError):ai.publish_job(job)

    def test_ai_invalid_citations_fail_and_input_scope_bounds(self):
        job=self.job();ai.confirm(job);data=structured();data["sources"][0]["id"]="invented"
        with patch("reading.ai.request_completion",return_value=(data,20)):ai.process_job(job.pk)
        job.refresh_from_db();self.assertEqual(job.status,"failed")
        with self.assertRaises(ValidationError):ai.draft(job.book,self.owner,job.provider,{"kind":"chapter","section":0,"excerpt":"不是书中内容"})

    def test_ai_only_own_notes_and_change_before_confirmation(self):
        job=self.job();note=self.annotation(job.book,"family")
        with self.assertRaises(ValidationError):ai.draft(job.book,self.peer,job.provider,{"kind":"notes","notes":[str(note.pk)]})
        notes_job=ai.draft(job.book,self.owner,job.provider,{"kind":"notes","notes":[str(note.pk)]})
        Annotation.objects.filter(pk=note.pk).update(revision=2,note="修改")
        with self.assertRaises(ValidationError):ai.confirm(notes_job)

    def test_new_pages_render_without_side_effects(self):
        job=self.job();version=self.publication(job.book)
        urls=[reverse("reading:plans"),self.url("ai_create",job.book),reverse("reading:ai_job",args=[job.pk]),
              reverse("reading:artifact",args=[version.artifact_id]),reverse("reading:artifact_edit",args=[version.artifact_id]),self.url("artifact_upload",job.book)]
        for url in urls:self.assertEqual(self.client.get(url).status_code,200,url)
        self.assertEqual(ReadingAiJob.objects.count(),1)
        self.assertEqual(ReadingArtifactVersion.objects.count(),1)
        self.client.force_login(self.peer.user)
        self.assertEqual(self.client.get(reverse("reading:ai_job",args=[job.pk])).status_code,404)

    def test_pdf_parser_geometry_and_scan_capabilities(self):
        import io
        from pypdf import PdfWriter
        from pypdf.generic import DictionaryObject,NameObject,DecodedStreamObject
        from reading.text import pdf_text
        writer=PdfWriter();page=writer.add_blank_page(width=600,height=800)
        font=DictionaryObject({NameObject("/Type"):NameObject("/Font"),NameObject("/Subtype"):NameObject("/Type1"),NameObject("/BaseFont"):NameObject("/Helvetica")})
        page[NameObject("/Resources")]=DictionaryObject({NameObject("/Font"):DictionaryObject({NameObject("/F1"):writer._add_object(font)})})
        stream=DecodedStreamObject();stream.set_data(b"BT /F1 16 Tf 50 700 Td (Reading text fixture) Tj ET")
        page[NameObject("/Contents")]=writer._add_object(stream)
        writer.add_blank_page(width=600,height=800);output=io.BytesIO();writer.write(output)
        book=self.upload("valid.pdf",output.getvalue());process_file(book.file.pk);book.refresh_from_db()
        self.assertEqual(book.file.status,"ready");self.assertEqual(book.file.page_count,2)
        self.assertEqual(pdf_text(book.file,2,2)["pages"][0]["text"],"")
        payload={"file_hash":book.file.sha256,"normalizer_version":book.file.normalizer_version,"quote":"Reading text fixture", "note":"PDF note",
                 "anchor":{"page":1,"rects":[[.1,.1,.3,.03]]}}
        response=self.client.post(self.url("annotations",book),payload,content_type="application/json")
        self.assertEqual(response.status_code,201,response.content);self.assertTrue(response.json()["text_matched"])
        payload["anchor"]["rects"]=[[.9,.1,.3,.03]]
        self.assertEqual(self.client.post(self.url("annotations",book),payload,content_type="application/json").status_code,400)
        with self.assertRaises(ValidationError):pdf_text(book.file,1,3)

    def test_html_replacement_preserves_old_file_and_direct_preview_downloads(self):
        book=self.ready();version=self.publication(book)
        old=version.original_path
        response=self.client.post(reverse("reading:artifact_edit",args=[version.artifact_id]),{
            "mode":"file","base_version":version.pk,"title":"新稿","file":SimpleUploadedFile("new.html",b"<html>new version</html>")})
        self.assertEqual(response.status_code,302)
        newest=version.artifact.versions.get(number=2)
        version.refresh_from_db();self.assertEqual(version.original_path,old)
        url=reverse("reading:artifact_file",args=[newest.artifact_id,2])
        response=self.client.get(url,{"preview":"1"})
        self.assertIn("attachment",response["Content-Disposition"])
        response=self.client.get(url,{"preview":"1"},HTTP_SEC_FETCH_DEST="iframe")
        self.assertEqual(response["Content-Type"],"text/html; charset=utf-8")
        self.assertEqual(response["X-Frame-Options"],"SAMEORIGIN")
        self.assertIn("frame-src 'self'",self.client.get(reverse("reading:artifact",args=[newest.artifact_id]))["Content-Security-Policy"])

    def test_artifact_revocation_and_cross_family_raw_access(self):
        book=self.ready();version=self.publication(book);doc=archive_version(version,self.owner)
        self.client.force_login(self.other.user)
        urls=[reverse("reading:artifact",args=[version.artifact_id]),reverse("reading:artifact_file",args=[version.artifact_id,1]),
              reverse("knowledge:document_detail",args=[doc.pk])]
        for url in urls:self.assertEqual(self.client.get(url).status_code,404)
        version.artifact.visibility="private";version.artifact.save()
        self.assertFalse(accessible_documents(self.peer).filter(pk=doc.pk).exists())
        self.assertTrue(accessible_documents(self.owner).filter(pk=doc.pk).exists())

    def test_ai_permission_revoked_and_stalled_job_not_resent(self):
        from datetime import timedelta
        from django.utils import timezone
        job=self.job();job.member=self.peer;job.save();ai.confirm(job)
        Book.objects.filter(pk=job.book_id).update(visibility="private")
        with patch("reading.ai.request_completion") as send:ai.process_job(job.pk);send.assert_not_called()
        job.refresh_from_db();self.assertEqual(job.status,"failed")
        ReadingAiJob.objects.filter(pk=job.pk).update(status="running",started_at=timezone.now()-timedelta(minutes=11))
        self.assertEqual(ai.expire_stalled_jobs(),1)
        self.assertFalse(ai.process_job(job.pk))

    def test_ai_transport_bounds_format_and_redirect_protection(self):
        import io
        from reading.ai import NoRedirect
        job=self.job()
        with self.assertRaises(ValidationError):NoRedirect().redirect_request(None,None,302,"",{},"https://elsewhere.example")
        payload=json.dumps({"choices":[{"finish_reason":"stop","message":{"content":json.dumps(structured())}}],"usage":{"total_tokens":100}}).encode()
        with patch("reading.ai._chat_url",return_value="https://example.com/v1/chat/completions"),patch("reading.ai._api_key",return_value="synthetic-test-only"),patch("reading.ai.build_opener") as opener:
            opener.return_value.open.return_value=io.BytesIO(payload)
            data,tokens=ai.request_completion(job);self.assertEqual(tokens,100)
            req=opener.return_value.open.call_args.args[0]
            body=json.loads(req.data);self.assertEqual(body["max_tokens"],2048)
            self.assertNotIn("tools",body)
            opener.return_value.open.return_value=io.BytesIO(b"x"*(512*1024+1))
            with self.assertRaises(ValidationError):ai.request_completion(job)
