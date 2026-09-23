import gzip
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from family_core.models import Family, FamilyMember
from portfolio.models import Security

from .models import OfficialResearchContentVersion, OfficialResearchDocument
from .providers.sec import SecClient, SecClientError, SecResponseTooLarge, _OfficialRedirectHandler
from .sec_content import extract_sec_html, fetch_sec_document_content
from .services import ResearchValidationError, create_dossier


class FakeSecClient:
    def __init__(self, body):
        self.body = body
        self.calls = []

    def get_document_html(self, url, *, max_bytes):
        self.calls.append((url, max_bytes))
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


HTML = (b"<html><head><style>NO STYLE</style></head><body>"
        b"<h1>Management discussion</h1><p>Revenue increased to $100 million.</p>"
        b"<table><tr><th>Metric</th><th>2026</th></tr>"
        b"<tr><td>Cash flow</td><td>80</td></tr></table>"
        b"<ix:header><ix:hidden>NO HIDDEN</ix:hidden></ix:header>"
        b"<script>NO SCRIPT</script></body></html>")


class SecContentTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        family = Family.objects.create(name="First")
        other_family = Family.objects.create(name="Second")
        cls.security = Security.objects.create(symbol="AAPL", name="Apple", market="US", asset_type="stock")
        user = get_user_model().objects.create_user(username="research-a", password="x")
        cls.actor = FamilyMember.objects.create(family=family, user=user, display_name="A")
        other_user = get_user_model().objects.create_user(username="research-b", password="x")
        cls.other = FamilyMember.objects.create(family=other_family, user=other_user, display_name="B")
        cls.dossier = create_dossier(actor=cls.actor, security=cls.security, initial_thesis="A thesis", pillars=[], questions=[])
        cls.document = OfficialResearchDocument.objects.create(
            security=cls.security, source="sec", external_id="0000320193-26-000001",
            document_type="10-k", title="Apple 10-K",
            source_url="https://www.sec.gov/Archives/edgar/data/320193/000032019326000001/apple.htm",
            metadata={"cik": "0000320193", "form": "10-K"},
        )

    def test_extract_preserves_tables_and_excludes_hidden_content(self):
        text = extract_sec_html(HTML)
        self.assertIn("Revenue increased to $100 million.", text)
        self.assertIn("Cash flow | 80", text)
        for unwanted in ("NO STYLE", "NO HIDDEN", "NO SCRIPT"):
            self.assertNotIn(unwanted, text)

    def test_nested_table_cell_blocks_keep_labels_and_values_on_one_row(self):
        html = (b"<html><body><table><tr><td><div>Total revenue</div></td>\n "
                b"<td>\n<div>331,839</div></td>\n<td><div>281,724</div></td></tr>"
                b"</table><p>Report body follows.</p></body></html>")
        text = extract_sec_html(html)
        self.assertIn("Total revenue | 331,839 | 281,724", text)

    def test_snapshot_is_immutable_and_repeated_content_is_idempotent(self):
        client = FakeSecClient(HTML)
        version, created = fetch_sec_document_content(
            actor=self.actor, dossier_id=self.dossier.pk, document_id=self.document.pk, client=client,
        )
        self.assertTrue(created)
        self.assertEqual(gzip.decompress(version.raw_gzip), HTML)
        self.assertEqual(version.version_number, 1)
        self.document.refresh_from_db()
        self.assertEqual(self.document.content_text, version.content_text)
        repeated, created = fetch_sec_document_content(
            actor=self.actor, dossier_id=self.dossier.pk, document_id=self.document.pk, client=client,
        )
        self.assertFalse(created)
        self.assertEqual(repeated.pk, version.pk)
        client.body = HTML.replace(b"$100", b"$120")
        revised, created = fetch_sec_document_content(
            actor=self.actor, dossier_id=self.dossier.pk, document_id=self.document.pk, client=client,
        )
        self.assertTrue(created)
        self.assertEqual(revised.version_number, 2)
        version.refresh_from_db()
        self.assertIn("$100", version.content_text)
        self.assertIn("$120", revised.content_text)
        self.assertEqual(OfficialResearchContentVersion.objects.count(), 2)

    def test_new_extractor_version_creates_new_snapshot_from_same_raw_file(self):
        client = FakeSecClient(HTML)
        old, _ = fetch_sec_document_content(
            actor=self.actor, dossier_id=self.dossier.pk, document_id=self.document.pk, client=client,
        )
        old.extractor_version = "sec-html-v1"
        old.save(update_fields=["extractor_version"])
        revised, created = fetch_sec_document_content(
            actor=self.actor, dossier_id=self.dossier.pk, document_id=self.document.pk, client=client,
        )
        self.assertTrue(created)
        self.assertEqual(revised.version_number, 2)
        self.assertEqual(revised.raw_sha256, old.raw_sha256)
        self.assertNotEqual(revised.pk, old.pk)

    def test_bad_url_non_html_and_failure_preserve_old_version(self):
        client = FakeSecClient(HTML)
        fetch_sec_document_content(actor=self.actor, dossier_id=self.dossier.pk, document_id=self.document.pk, client=client)
        original_text = OfficialResearchDocument.objects.get(pk=self.document.pk).content_text
        client.body = b"%PDF-1.5"
        with self.assertRaises(ResearchValidationError):
            fetch_sec_document_content(actor=self.actor, dossier_id=self.dossier.pk, document_id=self.document.pk, client=client)
        client.body = SecClientError("temporary")
        with self.assertRaises(SecClientError):
            fetch_sec_document_content(actor=self.actor, dossier_id=self.dossier.pk, document_id=self.document.pk, client=client)
        self.document.refresh_from_db()
        self.assertEqual(self.document.content_text, original_text)
        self.assertEqual(self.document.content_versions.count(), 1)
        self.document.source_url = "https://evil.example.com/file.htm"
        self.document.save(update_fields=["source_url"])
        with self.assertRaises(SecClientError):
            fetch_sec_document_content(actor=self.actor, dossier_id=self.dossier.pk, document_id=self.document.pk, client=client)
        self.assertEqual(len(client.calls), 3)

    def test_redirect_rejects_other_host_and_archive_escape(self):
        handler = _OfficialRedirectHandler()
        import urllib.request
        req = urllib.request.Request(self.document.source_url)
        with self.assertRaises(SecClientError):
            handler.redirect_request(req, None, 302, "moved", {}, "https://evil.example.com/payload")
        with self.assertRaises(SecClientError):
            handler.redirect_request(req, None, 302, "moved", {}, "https://www.sec.gov/files/other.htm")

    def test_all_three_filing_types_and_response_bound(self):
        for kind in ("10-k", "10-q", "8-k"):
            self.document.document_type = kind
            self.document.save(update_fields=["document_type"])
            version, _ = fetch_sec_document_content(
                actor=self.actor, dossier_id=self.dossier.pk,
                document_id=self.document.pk, client=FakeSecClient(HTML),
            )
            self.assertEqual(version.document_id, self.document.pk)

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, limit):
                return HTML[:limit]

        client = SecClient(user_agent="test/1.0", opener=lambda request, timeout: Response())
        with self.assertRaises(SecResponseTooLarge):
            client.get_document_html(self.document.source_url, max_bytes=20)

    def test_post_permissions_and_get_no_network(self):
        detail_url = reverse("investment_research:document_detail", args=[self.dossier.pk, self.document.pk])
        fetch_url = reverse("investment_research:fetch_sec_content", args=[self.dossier.pk, self.document.pk])
        with patch("investment_research.sec_content._default_sec_client") as factory:
            self.client.force_login(self.actor.user)
            self.assertEqual(self.client.get(detail_url).status_code, 200)
            self.assertEqual(self.client.get(fetch_url).status_code, 405)
            factory.assert_not_called()
            factory.return_value = FakeSecClient(HTML)
            self.assertEqual(self.client.post(fetch_url).status_code, 302)
            self.assertEqual(OfficialResearchContentVersion.objects.count(), 1)
            self.client.force_login(self.other.user)
            self.assertEqual(self.client.post(fetch_url).status_code, 404)
            factory.assert_called_once()
            self.actor.role = FamilyMember.ROLE_VIEWER
            self.actor.save(update_fields=["role"])
            self.client.force_login(self.actor.user)
            self.assertEqual(self.client.post(fetch_url).status_code, 403)
            factory.assert_called_once()

    def test_citation_stays_on_old_version_and_rejects_forgery(self):
        client = FakeSecClient(HTML)
        first, _ = fetch_sec_document_content(
            actor=self.actor, dossier_id=self.dossier.pk, document_id=self.document.pk, client=client,
        )
        self.client.force_login(self.actor.user)
        cite_url = reverse("investment_research:create_citation", args=[self.dossier.pk, self.document.pk])
        response = self.client.post(cite_url, {"version": first.pk, "quote": "Revenue increased to $100 million."})
        self.assertEqual(response.status_code, 302)
        params = parse_qs(urlsplit(response.url).query)
        self.assertEqual(params["version"], [str(first.pk)])
        client.body = HTML.replace(b"$100", b"$120")
        fetch_sec_document_content(actor=self.actor, dossier_id=self.dossier.pk, document_id=self.document.pk, client=client)
        citation = self.client.get(response.url)
        self.assertEqual(citation.status_code, 200)
        self.assertContains(citation, "Revenue increased to $100 million.")
        self.assertContains(citation, 'id="research-citation"')
        forged = response.url.replace(params["hash"][0], "0" * 64)
        self.assertEqual(self.client.get(forged).status_code, 404)
        self.assertEqual(self.client.get(response.url.split("?")[0] + "?version=bad").status_code, 404)
        self.assertEqual(self.client.post(cite_url, {"version": "bad", "quote": "Revenue"}).status_code, 404)
        self.client.force_login(self.other.user)
        self.assertEqual(self.client.get(response.url).status_code, 404)
        self.assertEqual(self.client.post(cite_url, {"version": first.pk, "quote": "Revenue"}).status_code, 404)
