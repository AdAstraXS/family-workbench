"""从探索档案到正式判断、后续财报复核和修订判断的完整流程。"""
import gzip
import hashlib
from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from family_core.models import Family, FamilyMember
from portfolio.models import Security

from .citations import resolve_quote
from .filing_review import FilingReviewConflict, save_filing_review
from .models import (
    OfficialResearchContentVersion, OfficialResearchDocument, ResearchFilingReview,
)
from .sec_content import extract_sec_html
from .services import create_exploration, save_thesis_revision


class FilingReviewJourneyTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        family = Family.objects.create(name="Review family")
        user = get_user_model().objects.create_user(username="review-owner", password="x")
        cls.member = FamilyMember.objects.create(family=family, user=user, display_name="Owner")
        other_user = get_user_model().objects.create_user(username="review-other", password="x")
        cls.other = FamilyMember.objects.create(family=family, user=other_user,
                                                display_name="Other")
        cls.security = Security.objects.create(symbol="REVIEW", name="Review Inc.", market="US",
                                               asset_type=Security.TYPE_STOCK)
        cls.dossier = create_exploration(actor=cls.member, security=cls.security)

    def filing(self, *, days_after, with_body=True):
        published = timezone.localdate() + timedelta(days=days_after)
        document = OfficialResearchDocument.objects.create(
            security=self.security, source="sec", external_id=f"review-{days_after}",
            document_type="10-q", title=f"Review 10-Q {days_after}",
            source_url=f"https://www.sec.gov/Archives/edgar/data/1/review-{days_after}.htm",
            published_at=published, period_end=published,
            metadata={"cik": "1"},
        )
        if not with_body:
            return document, None
        raw = ("<html><body><h1>Quarterly filing</h1>"
               "<p>Services revenue rose to 200 million dollars in the quarter.</p>"
               "<p>Other evidence.</p></body></html>").encode()
        text = extract_sec_html(raw)
        version = OfficialResearchContentVersion.objects.create(
            document=document, version_number=1, source_url=document.source_url,
            raw_sha256=hashlib.sha256(raw).hexdigest(), raw_gzip=gzip.compress(raw),
            content_text=text, content_sha256=hashlib.sha256(text.encode()).hexdigest(),
            extractor_version="test", fetched_at=timezone.now(),
        )
        return document, version

    def first_thesis(self):
        url = reverse("investment_research:first_thesis", args=[self.dossier.pk])
        response = self.client.post(url, {
            "thesis": "服务收入有望持续增长。", "pillars": "服务收入增长",
            "questions": "增长是否转化为现金？",
        })
        self.assertEqual(response.status_code, 302)
        self.dossier.refresh_from_db()
        return self.dossier.current_revision

    def review_payload(self, revision, version, *, quote=True):
        return {
            "expected_revision_id": revision.pk, "version_id": version.pk,
            "pillar_0": "supports", "pillar_0_note": "收入增加",
            "question_0": "unanswered", "question_0_note": "现金流仍需查看",
            "outcome": "mixed", "action": "revise", "summary": "收入有进展，现金回报待核查。",
            "quote": "Services revenue rose to 200 million dollars in the quarter." if quote else "",
            "follow_up": "核查经营现金流。",
        }

    def test_full_explore_thesis_next_filing_review_and_revision_journey(self):
        self.client.force_login(self.member.user)
        detail_url = reverse("investment_research:detail", args=[self.dossier.pk])
        self.assertContains(self.client.get(detail_url), "1 · 了解公司")
        self.assertContains(self.client.get(detail_url), "记录第一版判断")
        revision = self.first_thesis()
        old_document, _ = self.filing(days_after=-10)
        future, version = self.filing(days_after=10)
        reviews_url = reverse("investment_research:filing_reviews", args=[self.dossier.pk])
        review_url = reverse("investment_research:filing_review",
                             args=[self.dossier.pk, future.pk])
        self.assertContains(self.client.get(detail_url), "1 份新财报待复核")
        self.assertContains(self.client.get(reverse("investment_research:index")),
                            "1 份新财报待复核")
        self.assertContains(self.client.get(reviews_url), future.title)
        self.assertNotContains(self.client.get(reviews_url), old_document.title)
        self.assertContains(self.client.get(review_url), "查看保存的正文")
        self.assertEqual(ResearchFilingReview.objects.count(), 0)
        self.assertEqual(self.client.post(review_url, self.review_payload(revision, version)).status_code,
                         302)
        review = ResearchFilingReview.objects.get()
        self.assertEqual(review.thesis_revision_id, revision.pk)
        self.assertEqual([item["status"] for item in review.assessments],
                         ["supports", "unanswered"])
        self.assertIn("Services revenue", resolve_quote(
            version, review.citation["start"], review.citation["end"],
            review.citation["hash"],
        )[1])
        self.assertContains(self.client.get(detail_url), "待修订判断")
        self.assertContains(self.client.get(reverse("investment_research:index")),
                            "1 份复核提示修订判断")
        self.assertContains(self.client.get(review_url), "核对本次引用的原文")
        save_thesis_revision(
            actor=self.member, dossier_id=self.dossier.pk,
            expected_revision_id=revision.pk,
            thesis="服务收入增长，但现金回报仍需核查。", pillars=["现金回报"],
            questions=["下一期现金流如何？"], change_reason="复核后调整",
        )
        self.assertNotContains(self.client.get(detail_url), "待修订判断")
        self.assertNotContains(self.client.get(reverse("investment_research:index")),
                               "复核提示修订判断")
        review.refresh_from_db()
        self.assertEqual(review.thesis_revision_id, revision.pk)

    def test_review_requires_source_for_conclusion_and_rejects_stale_revision(self):
        self.client.force_login(self.member.user)
        revision = self.first_thesis()
        document, version = self.filing(days_after=10)
        url = reverse("investment_research:filing_review", args=[self.dossier.pk, document.pk])
        response = self.client.post(url, self.review_payload(revision, version, quote=False))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "请复制一段这份财报中的原文")
        self.assertEqual(ResearchFilingReview.objects.count(), 0)
        save_thesis_revision(
            actor=self.member, dossier_id=self.dossier.pk, expected_revision_id=revision.pk,
            thesis="更新判断", pillars=[], questions=[], change_reason="新证据",
        )
        with self.assertRaises(FilingReviewConflict):
            save_filing_review(
                actor=self.member, dossier_id=self.dossier.pk, document_id=document.pk,
                version_id=version.pk, expected_revision_id=revision.pk,
                assessments=[{"kind": "pillar", "text": "服务收入增长", "status": "supports",
                              "note": ""},
                             {"kind": "question", "text": "增长是否转化为现金？",
                              "status": "unanswered", "note": ""}],
                outcome="mixed", action="revise", summary="新证据", quote="Services revenue",
            )
        self.assertEqual(ResearchFilingReview.objects.count(), 0)

    def test_review_is_private_and_viewer_cannot_write(self):
        self.client.force_login(self.member.user)
        revision = self.first_thesis()
        document, version = self.filing(days_after=10)
        url = reverse("investment_research:filing_review", args=[self.dossier.pk, document.pk])
        self.client.force_login(self.other.user)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.post(url, self.review_payload(revision, version)).status_code,
                         404)
        self.member.role = FamilyMember.ROLE_VIEWER
        self.member.save(update_fields=["role"])
        self.client.force_login(self.member.user)
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(self.client.post(url, self.review_payload(revision, version)).status_code,
                         403)
