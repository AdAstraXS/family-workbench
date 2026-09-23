"""10-K 指标的金额、维度和固定原文引用拒错测试。"""
import gzip
from datetime import date
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from family_core.models import Family, FamilyMember
from portfolio.models import Security

from .citations import resolve_quote
from .models import OfficialResearchContentVersion, OfficialResearchDocument
from .sec_content import extract_sec_html
from .services import create_dossier
from .tenk_metrics import tenk_metric_rows


def _version(*, duplicate=False, dimensioned=False, visible=True, zero=False, wrong_unit=False):
    # 故意让同一个概念既有整公司事实，又有较大的分部事实。
    value = "—" if zero else "100"
    fmt = ' format="ixt:fixed-zero"' if zero else ""
    cash_line = (f"Net cash provided by operating activities | {value}"
                 if visible else "Operating activities were strong")
    unit = "shares" if wrong_unit else "usd"
    fact = (f'<ix:nonFraction name="us-gaap:NetCashProvidedByUsedInOperatingActivities" '
            f'contextRef="fy" unitRef="{unit}" scale="6" id="cash"{fmt}>{value}</ix:nonFraction>')
    repeat = fact.replace('id="cash"', 'id="cash-copy"') if duplicate else ""
    segment = (f'<ix:nonFraction name="us-gaap:NetCashProvidedByUsedInOperatingActivities" '
               f'contextRef="segment" unitRef="usd" scale="6" id="wrong">999</ix:nonFraction>') if dimensioned else ""
    raw = ("<html><body><ix:header>"
           "<xbrli:unit id='usd'><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>"
           "<xbrli:unit id='shares'><xbrli:measure>xbrli:shares</xbrli:measure></xbrli:unit>"
           "<xbrli:context id='fy'><xbrli:period><xbrli:startDate>2024-01-01</xbrli:startDate>"
           "<xbrli:endDate>2024-12-31</xbrli:endDate></xbrli:period></xbrli:context>"
           "<xbrli:context id='segment'><xbrli:entity><xbrli:segment>"
           "<xbrldi:explicitMember dimension='srt:ProductOrServiceAxis'>x:CloudMember</xbrldi:explicitMember>"
           "</xbrli:segment></xbrli:entity><xbrli:period><xbrli:startDate>2024-01-01</xbrli:startDate>"
           "<xbrli:endDate>2024-12-31</xbrli:endDate></xbrli:period></xbrli:context>"
           "</ix:header><h1>ITEM 8. FINANCIAL STATEMENTS AND SUPPLEMENTARY DATA</h1>"
           f"<table><tr><td>{cash_line.split(' | ')[0]}</td><td>{fact}</td></tr></table>"
           f"{repeat}{segment}<p>{'Other text ' * 100}</p>"
           "<h1>ITEM 9. CHANGES IN AND DISAGREEMENTS WITH ACCOUNTANTS</h1>"
           "</body></html>").encode()
    return SimpleNamespace(raw_gzip=gzip.compress(raw), content_text=extract_sec_html(raw),
                           document=SimpleNamespace(period_end=date(2024, 12, 31), document_type="10-k"))


class TenKMetricsTests(SimpleTestCase):
    def test_selects_full_year_usd_and_undimensioned_fact_with_fixed_quote(self):
        version = _version(dimensioned=True)
        rows, problem = tenk_metric_rows(version)
        self.assertIsNone(problem)
        cash = rows[0]
        self.assertEqual(cash["amount"], 1)
        self.assertEqual(cash["status"], "已核对")
        cited = resolve_quote(version, cash["citation"]["start"], cash["citation"]["end"],
                              cash["citation"]["hash"])
        self.assertIn("Net cash provided by operating activities | 100", cited[1])

    def test_repeated_identical_fact_is_not_summed_and_zero_is_distinct_from_absent(self):
        rows, _ = tenk_metric_rows(_version(duplicate=True, zero=True))
        self.assertEqual(rows[0]["amount"], 0)
        self.assertEqual(rows[0]["status"], "已核对")
        self.assertNotIn("amount", rows[1])

    def test_missing_original_label_hides_amount(self):
        rows, _ = tenk_metric_rows(_version(visible=False))
        self.assertEqual(rows[0]["status"], "原文引用待核对")
        self.assertNotIn("amount", rows[0])

    def test_wrong_unit_cannot_be_treated_as_usd(self):
        rows, _ = tenk_metric_rows(_version(wrong_unit=True))
        self.assertEqual(rows[0]["status"], "本文件未见匹配的 XBRL 事实")
        self.assertNotIn("amount", rows[0])


class TenKMetricsPageTests(TestCase):
    def test_private_page_uses_saved_version_without_writing(self):
        family = Family.objects.create(name="Family")
        security = Security.objects.create(symbol="TEST", name="Test", market="US", asset_type="stock")
        owner_user = get_user_model().objects.create_user(username="metric-owner", password="x")
        owner = FamilyMember.objects.create(family=family, user=owner_user, display_name="Owner")
        other_user = get_user_model().objects.create_user(username="metric-other", password="x")
        FamilyMember.objects.create(family=family, user=other_user, display_name="Other")
        dossier = create_dossier(actor=owner, security=security, initial_thesis="View filing",
                                 pillars=[], questions=[])
        document = OfficialResearchDocument.objects.create(
            security=security, source="sec", external_id="0000000000-24-000001",
            document_type="10-k", title="Test 10-K", period_end=date(2024, 12, 31),
            source_url="https://www.sec.gov/Archives/edgar/data/1/000000000024000001/test.htm",
            metadata={"cik": "1"},
        )
        source = _version()
        version = OfficialResearchContentVersion.objects.create(
            document=document, version_number=1, source_url=document.source_url,
            raw_sha256="a" * 64, raw_gzip=source.raw_gzip,
            content_text=source.content_text, content_sha256="b" * 64,
            extractor_version="sec-html-v2", fetched_at=timezone.now(),
        )
        url = reverse("investment_research:document_metrics", args=[dossier.pk, document.pk])
        self.client.force_login(owner_user)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "经营现金流")
        self.assertContains(response, f"version={version.pk}&amp;start=")
        self.assertEqual(OfficialResearchContentVersion.objects.count(), 1)
        self.client.force_login(other_user)
        self.assertEqual(self.client.get(url).status_code, 404)
