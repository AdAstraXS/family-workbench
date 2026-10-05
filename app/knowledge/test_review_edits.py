from bs4 import BeautifulSoup
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from family_core.models import Family, FamilyMember
from .models import KnowledgeDocument, KnowledgeProposal, KnowledgeProposalRun, KnowledgeRevision, KnowledgeSource


class ReviewEditedValuesTests(TestCase):
    def setUp(self):
        self.family = Family.objects.create(name="预览编辑测试")
        self.user = get_user_model().objects.create_user(username="review-edit-owner")
        self.member = FamilyMember.objects.create(family=self.family, user=self.user, display_name="测试成员")
        self.source = KnowledgeSource.objects.create(family=self.family, owner=self.member, key="review-edit", name="测试来源", kind="web_capture")
        self.doc = KnowledgeDocument.objects.create(family=self.family, owner=self.member, source=self.source, external_id="edit-doc", title="修改预览测试", library_tier="knowledge", knowledge_status="included", curation_status="pending_review", confirmed_summary="原正式摘要")
        self.rev = KnowledgeRevision.objects.create(document=self.doc, revision_number=1, content_hash="a" * 64, normalized_html="<p>原始正文</p>", plain_text="原始正文")
        self.doc.current_revision = self.rev
        self.doc.save()
        self.run = KnowledgeProposalRun.objects.create(document=self.doc, revision=self.rev, sequence=1, requested_by=self.member, model_name="test", prompt_version="test", content_hash=self.rev.content_hash)
        self.proposals = {}
        for kind, value in [("summary", {"text": "AI 原摘要"}), ("category", {"value": "AI 原分类"}), ("tags", {"items": ["AI 原标签"]})]:
            self.proposals[kind] = KnowledgeProposal.objects.create(document=self.doc, revision=self.rev, run=self.run, proposal_type=kind, suggested_value=value, model_name="test", prompt_version="test", content_hash=self.rev.content_hash)
        self.edits = {"summary": "人工改写的摘要 <script>只是文字</script>", "category": "人工分类", "tags": "研究，长期持有"}
        self.client.force_login(self.user)

    def payload(self, apply=False):
        values = {f"value_{proposal.pk}": self.edits[kind] for kind, proposal in self.proposals.items()}
        values["use_edited_values"] = "yes"
        if apply:
            values.update(proposal_ids=",".join(str(p.pk) for p in self.proposals.values()), confirm="yes")
        else:
            values["selected"] = [p.pk for p in self.proposals.values()]
        return values

    def test_preview_uses_edits_without_writing_formal_or_ai_values(self):
        before = self.doc.updated_at
        response = self.client.post(reverse("knowledge:proposal_bulk_preview"), self.payload())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "人工改写的摘要")
        self.assertContains(response, "人工分类")
        self.assertContains(response, "研究，长期持有")
        self.assertNotContains(response, "AI 原摘要")
        self.assertContains(response, "&lt;script&gt;")
        self.doc.refresh_from_db()
        self.assertEqual((self.doc.confirmed_summary, self.doc.updated_at), ("原正式摘要", before))
        for proposal in self.proposals.values():
            proposal.refresh_from_db()
            self.assertEqual(proposal.status, "pending")
        self.assertEqual(self.proposals["summary"].suggested_value, {"text": "AI 原摘要"})

    def test_bulk_confirmation_saves_edited_values_and_preserves_ai_originals(self):
        response = self.client.post(reverse("knowledge:proposal_bulk_apply"), self.payload(apply=True))
        self.assertRedirects(response, reverse("knowledge:review"))
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.confirmed_summary, self.edits["summary"])
        self.assertEqual(self.doc.category, "人工分类")
        self.assertEqual(self.doc.tags, ["研究", "长期持有"])
        self.assertEqual(self.doc.curation_status, "confirmed")
        self.assertEqual(self.doc.curation_revisions.count(), 1)
        summary = self.proposals["summary"]
        summary.refresh_from_db()
        self.assertEqual(summary.human_value, {"value": self.edits["summary"]})
        self.assertEqual(summary.suggested_value, {"text": "AI 原摘要"})
        self.rev.refresh_from_db()
        self.assertEqual(self.rev.plain_text, "原始正文")

    def test_single_confirmation_uses_native_shared_form_value_for_only_one_proposal(self):
        proposal = self.proposals["summary"]
        data = self.payload()
        data.update(action="accept", return_to="review")
        response = self.client.post(reverse("knowledge:proposal_review", args=[proposal.pk]), data)
        self.assertRedirects(response, reverse("knowledge:review"))
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.confirmed_summary, self.edits["summary"])
        self.assertEqual(self.doc.proposals.filter(status="pending").count(), 2)

    def test_missing_or_empty_edits_reject_entire_confirmation(self):
        for mode in ["missing", "empty"]:
            data = self.payload(apply=True)
            key = f"value_{self.proposals['category'].pk}"
            if mode == "missing":
                del data[key]
            else:
                data[key] = "  "
            response = self.client.post(reverse("knowledge:proposal_bulk_apply"), data)
            self.assertEqual(response.status_code, 302)
            self.assertEqual(self.doc.proposals.filter(status="pending").count(), 3)
            self.doc.refresh_from_db()
            self.assertEqual(self.doc.confirmed_summary, "原正式摘要")
        data = self.payload()
        del data[f"value_{self.proposals['category'].pk}"]
        self.assertEqual(self.client.post(reverse("knowledge:proposal_bulk_preview"), data).status_code, 302)

    def test_version_change_after_preview_prevents_all_writes(self):
        self.client.post(reverse("knowledge:proposal_bulk_preview"), self.payload())
        new = KnowledgeRevision.objects.create(document=self.doc, revision_number=2, content_hash="b" * 64)
        self.doc.current_revision = new
        self.doc.save()
        response = self.client.post(reverse("knowledge:proposal_bulk_apply"), self.payload(apply=True))
        self.assertEqual(response.status_code, 302)
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.confirmed_summary, "原正式摘要")
        self.assertEqual(self.doc.proposals.filter(status="pending").count(), 3)

    def test_other_member_and_viewer_cannot_apply_edits(self):
        for role in ["member", "viewer"]:
            user = get_user_model().objects.create_user(username=f"review-edit-{role}")
            FamilyMember.objects.create(family=self.family, user=user, display_name=role, role=role)
            self.client.force_login(user)
            self.assertIn(self.client.post(reverse("knowledge:proposal_bulk_apply"), self.payload(apply=True)).status_code, [302, 403])
            self.assertEqual(self.doc.proposals.filter(status="pending").count(), 3)

    def test_native_form_ownership_in_detail_and_review_preserves_editor_values(self):
        for url in [reverse("knowledge:document_detail", args=[self.doc.pk]), reverse("knowledge:review")]:
            page = self.client.get(url)
            self.assertEqual(page.status_code, 200)
            soup = BeautifulSoup(page.content, "html.parser")
            form = soup.select_one("form#knowledge-bulk-review")
            self.assertIsNotNone(form)
            for proposal in self.proposals.values():
                textarea = soup.select_one(f'textarea[name="value_{proposal.pk}"]')
                self.assertEqual(textarea["form"], form["id"])
            self.assertFalse(soup.select("form form"))
            self.assertEqual(len(form.select('input[name="selected"]')), 3)

    def test_detail_sections_fold_metadata_but_keep_suggestions_open(self):
        page = self.client.get(reverse("knowledge:document_detail", args=[self.doc.pk]))
        soup = BeautifulSoup(page.content, "html.parser")
        sections = {d.select_one("summary").get_text(strip=True): d for d in soup.select("details.knowledge-detail-section")}
        self.assertNotIn("open", sections["文档信息"].attrs)
        self.assertNotIn("open", sections["来源与原文"].attrs)
        self.assertIn("open", sections["本次整理建议"].attrs)
        self.assertContains(page, 'aria-label="文档信息与整理建议"')

    def test_final_preview_allows_further_edits_and_submits_them(self):
        preview = self.client.post(reverse("knowledge:proposal_bulk_preview"), self.payload())
        soup = BeautifulSoup(preview.content, "html.parser")
        field = soup.select_one(f'textarea[name="value_{self.proposals["summary"].pk}"]')
        self.assertEqual(field["form"], "knowledge-bulk-apply")
        data = self.payload(apply=True)
        data[f"value_{self.proposals['summary'].pk}"] = "在预览页再次修改的摘要"
        self.client.post(reverse("knowledge:proposal_bulk_apply"), data)
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.confirmed_summary, "在预览页再次修改的摘要")
