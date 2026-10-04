from bs4 import BeautifulSoup
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from family_core.models import Family, FamilyMember
from intelligence.models import IntelligenceSubject, SubjectFollow, SubjectKnowledgeIdentity
from .models import KnowledgeDocument, KnowledgeRevision, KnowledgeSource
from .search import index_document


class PersonProfileTests(TestCase):
    def setUp(self):
        self.family = Family.objects.create(name="人物测试家庭")
        self.user = get_user_model().objects.create_user(username="person-admin")
        self.member = FamilyMember.objects.create(family=self.family, user=self.user, display_name="管理员", role="admin")
        self.subject = IntelligenceSubject.objects.create(
            subject_type="person", canonical_name="Test Author", display_name="测试人物", category="investor",
            profile_summary="已有简介",
        )
        self.identity = SubjectKnowledgeIdentity.objects.create(family=self.family, subject=self.subject, author_name="旧署名")
        self.client.force_login(self.user)

    def payload(self, **changes):
        data = {"canonical_name": "Test Author", "display_name": "测试人物", "category": "investor", "aliases": "其它称呼", "profile_summary": "更新简介", "knowledge_author_names": "旧署名\n新署名"}
        data.update(changes)
        return data

    def test_edit_retains_identity_slug_and_news_follow_state(self):
        slug = self.subject.slug
        response = self.client.post(reverse("knowledge:person_edit", args=[slug]), self.payload(canonical_name="forged-name"))
        self.assertRedirects(response, reverse("knowledge:people") + f"?subject={slug}")
        self.subject.refresh_from_db()
        self.identity.refresh_from_db()
        self.assertEqual(self.subject.canonical_name, "Test Author")
        self.assertEqual(self.subject.slug, slug)
        self.assertEqual(self.subject.profile_summary, "更新简介")
        self.assertTrue(self.identity.is_active)
        self.assertEqual(SubjectFollow.objects.count(), 0)
        self.assertEqual(set(self.subject.knowledge_identities.values_list("author_name", flat=True)), {"旧署名", "新署名"})

    def test_edit_preserves_original_document_and_author(self):
        source = KnowledgeSource.objects.create(family=self.family, owner=self.member, key="person-test", kind="markdown_import", name="历史资料")
        document = KnowledgeDocument.objects.create(family=self.family, owner=self.member, source=source, external_id="article", title="原文文章", author="旧署名")
        revision = KnowledgeRevision.objects.create(document=document, revision_number=1, content_hash="a" * 64, normalized_html="<p>原文</p>", plain_text="原文")
        document.current_revision = revision
        document.save()
        index_document(document)
        self.client.post(reverse("knowledge:person_edit", args=[self.subject.slug]), self.payload())
        document.refresh_from_db()
        revision.refresh_from_db()
        self.assertEqual(document.author, "旧署名")
        self.assertEqual(document.current_revision_id, revision.pk)
        self.assertEqual(revision.plain_text, "原文")
        response = self.client.get(reverse("knowledge:people"), {"subject": self.subject.slug})
        self.assertContains(response, "原文文章")
        self.assertContains(response, reverse("knowledge:person_edit", args=[self.subject.slug]))
        self.assertNotContains(response, "查看最新动态")

    def test_create_person_does_not_start_news_subscription(self):
        response = self.client.post(reverse("knowledge:person_create"), self.payload(canonical_name="New Author", display_name="新人物", knowledge_author_names="新作者"))
        self.assertEqual(response.status_code, 302)
        person = IntelligenceSubject.objects.get(canonical_name="New Author")
        self.assertEqual(person.subject_type, "person")
        self.assertTrue(SubjectKnowledgeIdentity.objects.filter(family=self.family, subject=person, author_name="新作者").exists())
        self.assertEqual(SubjectFollow.objects.count(), 0)

    def test_create_prefills_existing_author_without_writing(self):
        response = self.client.get(reverse("knowledge:person_create"), {"display_name": "作者姓名", "knowledge_author_name": "历史笔名"})
        self.assertEqual(response.context["form"].initial["canonical_name"], "作者姓名")
        self.assertEqual(response.context["form"].initial["knowledge_author_names"], "历史笔名")
        self.assertEqual(IntelligenceSubject.objects.count(), 1)

    def test_author_conflict_is_rejected_without_partial_profile_change(self):
        other = IntelligenceSubject.objects.create(subject_type="person", canonical_name="Other", display_name="另一人物", category="other")
        SubjectKnowledgeIdentity.objects.create(family=self.family, subject=other, author_name="占用署名")
        response = self.client.post(reverse("knowledge:person_edit", args=[self.subject.slug]), self.payload(knowledge_author_names="占用署名"))
        self.assertContains(response, "已连接到另一人物")
        self.subject.refresh_from_db()
        self.assertEqual(self.subject.profile_summary, "已有简介")
        self.assertTrue(SubjectKnowledgeIdentity.objects.get(pk=self.identity.pk).is_active)

    def test_empty_author_mapping_is_rejected(self):
        response = self.client.post(reverse("knowledge:person_edit", args=[self.subject.slug]), self.payload(knowledge_author_names=""))
        self.assertEqual(response.status_code, 200)
        self.assertIn("knowledge_author_names", response.context["form"].errors)
        self.assertTrue(SubjectKnowledgeIdentity.objects.get(pk=self.identity.pk).is_active)

    def test_read_only_member_can_read_profiles_but_cannot_edit(self):
        self.member.role = "viewer"
        self.member.save()
        self.assertContains(self.client.get(reverse("knowledge:person_profiles")), "测试人物")
        self.assertNotContains(self.client.get(reverse("knowledge:person_profiles")), "新增人物")
        self.assertEqual(self.client.get(reverse("knowledge:person_edit", args=[self.subject.slug])).status_code, 403)
        self.assertEqual(self.client.post(reverse("knowledge:person_create"), self.payload()).status_code, 403)

    def test_other_family_person_is_hidden_and_cannot_be_edited(self):
        other_family = Family.objects.create(name="另一个家庭")
        other = IntelligenceSubject.objects.create(subject_type="person", canonical_name="Private person", display_name="其它家庭人物", profile_summary="其它家庭简介", category="other")
        SubjectKnowledgeIdentity.objects.create(family=other_family, subject=other, author_name="其它家庭署名")
        self.assertNotContains(self.client.get(reverse("knowledge:person_profiles")), other.display_name)
        self.assertEqual(self.client.get(reverse("knowledge:person_edit", args=[other.slug])).status_code, 404)
        self.assertEqual(self.client.post(reverse("knowledge:person_edit", args=[other.slug]), self.payload()).status_code, 404)
        response = self.client.get(reverse("knowledge:people"), {"subject": other.slug})
        self.assertNotContains(response, other.profile_summary)
        self.assertIsNone(response.context["selected_subject"])

    def test_shared_person_only_updates_this_familys_author_mapping(self):
        other_family = Family.objects.create(name="另一个家庭")
        other_identity = SubjectKnowledgeIdentity.objects.create(family=other_family, subject=self.subject, author_name="另一家庭署名")
        response = self.client.post(reverse("knowledge:person_edit", args=[self.subject.slug]), self.payload(display_name="更改共用名字"))
        self.assertEqual(response.status_code, 302)
        self.subject.refresh_from_db()
        other_identity.refresh_from_db()
        self.assertEqual(self.subject.display_name, "测试人物")
        self.assertEqual(self.subject.profile_summary, "已有简介")
        self.assertTrue(other_identity.is_active)
        self.assertEqual(other_identity.author_name, "另一家庭署名")
        self.assertNotContains(self.client.get(reverse("knowledge:person_profiles")), "另一家庭署名")

    def test_people_pages_get_does_not_write(self):
        for url in [reverse("knowledge:people") + f"?subject={self.subject.slug}", reverse("knowledge:person_profiles"), reverse("knowledge:person_edit", args=[self.subject.slug])]:
            with self.subTest(url=url), CaptureQueriesContext(connection) as queries:
                response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            writes = [q["sql"] for q in queries if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))]
            self.assertEqual(writes, [])

    def test_people_routes_keep_knowledge_navigation(self):
        response = self.client.get(reverse("knowledge:person_edit", args=[self.subject.slug]))
        soup = BeautifulSoup(response.content, "html.parser")
        active = soup.select('nav[aria-label="主导航"] a[aria-current="page"]')
        self.assertEqual([a.get("href") for a in active], [reverse("knowledge:index")])
        self.assertNotIn("AI 情报", soup.select_one('nav[aria-label="面包屑"]').get_text())
