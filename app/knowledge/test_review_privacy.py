from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from family_core.models import Family, FamilyMember
from notes.models import InvestmentNote, InvestmentNoteType
from .models import KnowledgeDocument, KnowledgeRevision, KnowledgeSource
from .permissions import accessible_documents, accessible_search_entries


class AuthoritativePrivacyTests(TestCase):
    def setUp(self):
        self.family = Family.objects.create(name="Synthetic privacy test")
        self.owner_user = get_user_model().objects.create_superuser(username="privacy-owner", password=None)
        self.owner = FamilyMember.objects.create(family=self.family, user=self.owner_user, display_name="Owner")
        self.viewer_user = get_user_model().objects.create_superuser(username="privacy-admin", password=None)
        self.viewer = FamilyMember.objects.create(family=self.family, user=self.viewer_user, display_name="Admin", role="admin")
        self.source = KnowledgeSource.objects.create(family=self.family, owner=self.owner, key="synthetic",
            kind="onenote", name="Synthetic source", visibility="family")
        self.document = KnowledgeDocument.objects.create(family=self.family, owner=self.owner,
            source=self.source, external_id="synthetic-1", title="SYNTHETIC_PRIVATE_TITLE", visibility="family",
            knowledge_status=KnowledgeDocument.KNOWLEDGE_INCLUDED)
        revision = KnowledgeRevision.objects.create(document=self.document, revision_number=1,
            content_hash="b" * 64, raw_file="synthetic-unopened.html", plain_text="SYNTHETIC_PRIVATE_BODY")
        self.document.current_revision = revision
        self.document.save()
        note_type, _ = InvestmentNoteType.objects.get_or_create(code="privacy", defaults={"name": "Synthetic"})
        self.note = InvestmentNote.objects.create(family=self.family, member=self.owner, note_type=note_type,
            title="SYNTHETIC_NOTE", content="SYNTHETIC_PRIVATE_NOTE_BODY", visibility="family", include_in_knowledge=True)

    def test_source_privacy_is_immediate_without_reindex(self):
        self.assertTrue(accessible_search_entries(self.viewer).filter(document=self.document).exists())
        KnowledgeSource.objects.filter(pk=self.source.pk).update(visibility="private")
        self.assertFalse(accessible_documents(self.viewer).filter(pk=self.document.pk).exists())
        self.assertFalse(accessible_search_entries(self.viewer).filter(document=self.document).exists())
        self.assertTrue(accessible_search_entries(self.owner).filter(document=self.document).exists())
        self.client.force_login(self.viewer_user)
        response = self.client.get(reverse("knowledge:library"), {"member": "all", "q": "PRIVATE_BODY"})
        self.assertNotContains(response, "SYNTHETIC_PRIVATE_BODY")
        self.assertNotContains(response, "SYNTHETIC_PRIVATE_TITLE")

    def test_document_and_note_privacy_override_stale_index(self):
        KnowledgeDocument.objects.filter(pk=self.document.pk).update(visibility="private")
        InvestmentNote.objects.filter(pk=self.note.pk).update(visibility="private")
        self.assertFalse(accessible_search_entries(self.viewer).exists())
        self.assertEqual(accessible_search_entries(self.owner).count(), 2)
        InvestmentNote.objects.filter(pk=self.note.pk).update(include_in_knowledge=False)
        self.assertEqual(accessible_search_entries(self.owner).count(), 1)

    def test_admin_cannot_bypass_private_notes_documents_or_projection(self):
        KnowledgeSource.objects.filter(pk=self.source.pk).update(visibility="private")
        InvestmentNote.objects.filter(pk=self.note.pk).update(visibility="private")
        self.client.force_login(self.viewer_user)
        for model, obj in [("notes_investmentnote", self.note), ("knowledge_knowledgedocument", self.document)]:
            response = self.client.get(reverse(f"admin:{model}_change", args=[obj.pk]))
            self.assertIn(response.status_code, (302, 403, 404))
            self.assertNotIn(b"SYNTHETIC_PRIVATE", response.content)
        response = self.client.get(reverse("admin:knowledge_knowledgesearchentry_changelist"))
        self.assertNotContains(response, "SYNTHETIC_PRIVATE_TITLE")
        self.assertNotContains(response, "SYNTHETIC_NOTE")
        self.client.force_login(self.owner_user)
        response = self.client.get(reverse("admin:notes_investmentnote_change", args=[self.note.pk]))
        self.assertContains(response, "SYNTHETIC_PRIVATE_NOTE_BODY")
        self.assertNotContains(response, 'name="_save"')

    def test_unbound_superuser_cannot_inspect_private_content(self):
        user = get_user_model().objects.create_superuser(username="unbound-admin", password=None)
        self.client.force_login(user)
        response = self.client.get(reverse("admin:notes_investmentnote_change", args=[self.note.pk]))
        self.assertIn(response.status_code, (302, 403, 404))
        self.assertNotIn(b"SYNTHETIC_PRIVATE_NOTE_BODY", response.content)

    def test_ai_request_and_result_admin_respect_request_owner(self):
        from ai_analysis.models import AiAnalysisRequest, AiAnalysisResult
        analysis = AiAnalysisRequest.objects.create(family=self.family, member=self.owner,
            module="knowledge", prompt="SYNTHETIC_PRIVATE_PROMPT")
        result = AiAnalysisResult.objects.create(request=analysis, result_text="SYNTHETIC_PRIVATE_RESULT")
        self.client.force_login(self.viewer_user)
        for model, obj in [("aianalysisrequest", analysis), ("aianalysisresult", result)]:
            response = self.client.get(reverse(f"admin:ai_analysis_{model}_change", args=[obj.pk]))
            self.assertIn(response.status_code, (302, 403, 404))
            self.assertNotIn(b"SYNTHETIC_PRIVATE", response.content)

    def test_filtered_library_link_retains_search_context(self):
        self.client.force_login(self.owner_user)
        url = reverse("knowledge:library") + "?collection=archive&member=all&q=SYNTHETIC"
        response = self.client.get(url)
        from urllib.parse import quote
        self.assertContains(response, "?return_to=" + quote(url, safe="/"))
